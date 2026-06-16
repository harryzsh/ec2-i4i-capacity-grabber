#!/usr/bin/env python3
"""Grab g7e.48xlarge capacity via On-Demand Capacity Reservations (COUNT-based).

Region is configurable via --region (default: us-east-1).

This is the count-based sibling of the repo's i4i grabber. The i4i script
counts vCPU; this one counts INSTANCES (台数). There is exactly ONE instance
type — g7e.48xlarge — so every unit is "one g7e.48xlarge", and the stop-gates
are instance counts, not cores.

Strategy: sweep across AZs, CreateCapacityReservation count=1 each
(all-or-nothing per call, so count=1 scavenges single-instance fragments),
tag each reservation, count INSTANCES toward a target, stop at the cap.

WATCH MODE (--watch): loop forever, re-sweeping every --interval seconds.
Capacity is intermittent, so 24x7 watching is how you actually catch it.
Every real reservation is logged and appended to logs/grabs.jsonl.

RESUME / IDEMPOTENCY: the per-AZ and total stop-gates read what we ACTUALLY
hold from AWS each round (held_count_by_az), counting INSTANCES
(sum of TotalInstanceCount) not reservation objects. So a crash/restart/
host-reboot picks up exactly where it left off: each AZ is judged against its
own real held instance count, and the sweep only tops up the true shortfall
per AZ. No in-memory counter to lose, no double-grab, no lopsided distribution
after a restart. Safe under systemd Restart=always.

Why ODCR over plain On-Demand: a reservation HOLDS the slot even when no
instance occupies it (and across stop/terminate/ASG-rollover). Trade-off:
an ACTIVE reservation bills at the On-Demand rate whether filled or not.
(Capacity Blocks for ML do NOT cover the G family, so ODCR is the tool here.)

SAFETY: default is --dry-run (validates IAM + params, reserves nothing).
Use --live to actually reserve. Use --cancel-all to release everything.
Immediate-use reservations here have NO commitment and cancel anytime.

Examples:
  python3 grab_g7e_odcr.py --target-count 1                      # dry-run plan
  python3 grab_g7e_odcr.py --target-count 4 --live               # really reserve
  python3 grab_g7e_odcr.py --azs us-east-1b us-east-1d --per-az-count 2 --live --watch
  python3 grab_g7e_odcr.py --cancel-all --live                   # release all
  python3 grab_g7e_odcr.py --list                                # show reservations
"""
import argparse
import sys
import time
import json
import datetime

from botocore.exceptions import ClientError

from common import (
    DEFAULT_REGION, INSTANCE_TYPE, VCPU_PER, TAG_KEY, TAG_VAL, GRAB_LEDGER,
    ec2_client, list_azs, offered_in_azs, backoff_sleep, classify,
    setup_logging, record_grab, resolve_azs,
)

# --target-count placeholder: a tiny value treated as "unset" so balanced mode
# (per-az-count x #AZ) can auto-fill the real total. Mirrors the i4i grabber's
# "default 8 = untouched" convention, but count-based so the default is 1.
DEFAULT_TARGET = 1

# Logging is ALWAYS on (console + rotating file) — the fallback record of truth.
log = setup_logging("grab_g7e_odcr.log")


def reserve_one(client, az, dry_run, end_hours=None):
    """Create a count=1 g7e.48xlarge ODCR. open matching, no commitment.

    end_hours: if set, EndDateType=limited (auto-expires) as a billing guard.
               if None, EndDateType=unlimited (until you cancel).
    """
    kwargs = dict(
        InstanceType=INSTANCE_TYPE,
        InstancePlatform="Linux/UNIX",
        AvailabilityZone=az,
        InstanceCount=1,
        InstanceMatchCriteria="open",
        Tenancy="default",
        # g7e is a Nitro family — EBS optimization is always-on and can't be
        # disabled. Mark the reservation EBS-optimized so its attributes match
        # the instances it will hold. NOTE: EbsOptimized is NOT one of the
        # `open` match criteria (those are instance type / platform / AZ /
        # tenancy only), so this neither helps nor blocks matching — it's set
        # for honest attribute alignment, not for placement.
        EbsOptimized=True,
        DryRun=dry_run,
        TagSpecifications=[{
            "ResourceType": "capacity-reservation",
            "Tags": [{"Key": TAG_KEY, "Value": TAG_VAL}],
        }],
    )
    if end_hours:
        end = datetime.datetime.utcnow() + datetime.timedelta(hours=end_hours)
        kwargs["EndDateType"] = "limited"
        kwargs["EndDate"] = end
    else:
        kwargs["EndDateType"] = "unlimited"
    return client.create_capacity_reservation(**kwargs)


def list_reservations(client):
    resp = client.describe_capacity_reservations(Filters=[
        {"Name": "state", "Values": ["active", "pending", "assessing"]},
    ])
    rows = []
    for cr in resp["CapacityReservations"]:
        tags = {t["Key"]: t["Value"] for t in cr.get("Tags", [])}
        rows.append((
            cr["CapacityReservationId"], cr["InstanceType"],
            cr["AvailabilityZone"], cr["State"],
            cr.get("TotalInstanceCount"), tags.get(TAG_KEY, ""),
            cr.get("AvailableInstanceCount"),   # 7th: free slots (Total - used)
        ))
    return rows


def held_count_by_az(client, only_azs=None):
    """Sum INSTANCES per AZ across THIS script's tagged reservations, read LIVE
    from AWS. Counts instances (TotalInstanceCount), NOT the number of
    reservation objects — one reservation can hold many instances, so counting
    objects would be meaningless.

    only_azs: if given (a set/list of AZ names), count ONLY those AZs. This
        keeps the stop-gate in scope when you target a subset of AZs: e.g.
        `--azs us-east-1d` must not let stock already held in us-east-1b
        inflate the total and stop the run before 1d is filled.

    This is the stop-gate's source of truth. Re-reading it every round is what
    makes restarts safe: after a crash we see exactly what we already hold, per
    AZ, and only top up the real shortfall — never double-grab, never lopsided.
    """
    held = {}
    for _crid, itype, az, _state, cnt, tag, _avail in list_reservations(client):
        if tag != TAG_VAL or itype != INSTANCE_TYPE:
            continue
        if only_azs is not None and az not in only_azs:
            continue
        held[az] = held.get(az, 0) + (cnt or 0)
    return held


def _targets_from_ledger():
    """Read the most recent (target_count, per_az_count) from grabs.jsonl.

    So `--list` ALONE can show progress — it remembers what target you were
    grabbing toward, no need to re-type --target-count / --per-az-count.
    Returns (target_count, per_az_count), each None if unavailable.
    """
    try:
        with open(GRAB_LEDGER) as f:
            lines = [ln for ln in f if ln.strip()]
        if not lines:
            return None, None
        last = json.loads(lines[-1])
        return last.get("target_count"), last.get("per_az_count")
    except (FileNotFoundError, ValueError, KeyError):
        return None, None


def print_list(client, target_count=None, per_az_count=None):
    """--list: show every tagged reservation, then a per-AZ + total summary.

    The summary answers the two questions you actually have during a grab:
    "how many instances do I hold total?" and "how is it split across AZs?"

    Each row also shows USED/free — whether an instance is actually occupying
    that reservation (Total - Available > 0) — and the summary tallies how many
    reservations are USED out of the total we hold.

    Progress (held/target + FULL/short) is shown automatically: if you don't
    pass --target-count / --per-az-count, they're read from the last grab in
    grabs.jsonl — so plain `--list` already shows how close you are.
    """
    # Fall back to the ledger's last-known targets when caller didn't pass any.
    if target_count is None and per_az_count is None:
        target_count, per_az_count = _targets_from_ledger()
    rows = list_reservations(client)
    if not rows:
        log.info("no active/pending reservations")
        return
    used_n = 0   # reservations with an instance actually IN them (used > 0)
    ours_n = 0   # our tagged reservations (the denominator)
    for crid, itype, az, state, cnt, tag, avail in rows:
        # USED = is an instance occupying this reservation? (Total - Available)
        total = cnt or 0
        free = avail if avail is not None else total
        used = total - free
        used_str = "USED" if used > 0 else "free"
        log.info("%s  %-14s %-12s %-9s count=%s %-4s tag=%s",
                 crid, itype, az, state, cnt, used_str, tag)
        if tag == TAG_VAL:
            ours_n += 1
            if used > 0:
                used_n += 1
    # Summary: only OUR tagged g7e.48xlarge reservations, counted in INSTANCES.
    held = held_count_by_az(client)
    if held:
        log.info("--- summary (tag=%s) ---", TAG_VAL)
        for az in sorted(held):
            got = held[az]
            if per_az_count:
                flag = "FULL" if got >= per_az_count else "short"
                log.info("  %-12s %4d / %d %s [%s]",
                         az, got, per_az_count, INSTANCE_TYPE, flag)
            else:
                log.info("  %-12s %4d x %s", az, got, INSTANCE_TYPE)
        total = sum(held.values())
        if target_count:
            flag = "FULL" if total >= target_count else "short"
            log.info("  %-12s %4d / %d instances across %d AZ(s) [%s]",
                     "TOTAL", total, target_count, len(held), flag)
        else:
            log.info("  %-12s %4d instances across %d AZ(s)",
                     "TOTAL", total, len(held))
        # How many reservations actually have an instance in them.
        log.info("  %-12s %d / %d reservations USED (have an instance running)",
                 "USED", used_n, ours_n)


def cancel_all(client, dry_run):
    rows = [r for r in list_reservations(client) if r[5] == TAG_VAL]
    if not rows:
        log.info("no tagged reservations to cancel")
        return
    for crid, itype, az, state, cnt, _tag, _avail in rows:
        log.info("cancel %s (%s @ %s, %s)", crid, itype, az, state)
        if dry_run:
            log.info("  [dry-run] would cancel")
            continue
        try:
            client.cancel_capacity_reservation(CapacityReservationId=crid)
            log.info("  cancelled")
        except ClientError as e:
            log.error("  cancel failed: %s", e.response.get("Error"))


def _on_grab(args, crid, az, held, made):
    """Bookkeeping for one secured reservation: bump held count, log, ledger.

    held is the per-AZ instance gate (seeded from AWS truth each round); we bump
    it locally so the gate stays accurate WITHIN a sweep, between AWS re-reads.
    Each ODCR is count=1, so every grab adds exactly one instance.
    """
    held[az] = held.get(az, 0) + 1
    made.append((crid, INSTANCE_TYPE, az))
    total = sum(held.values())
    if args.per_az_count:
        log.info("RESERVED %s %s @ %s (+1 | %s: %d/%d | total %d/%d)",
                 crid, INSTANCE_TYPE, az, az, held[az],
                 args.per_az_count, total, args.target_count)
    else:
        log.info("RESERVED %s %s @ %s (+1, total %d/%d)",
                 crid, INSTANCE_TYPE, az, total, args.target_count)
    record_grab("odcr", az, 1, total, args.target_count, args.region,
                not args.live, per_az_count=args.per_az_count,
                per_az_total=held[az])


def _az_full(args, held, az):
    """True if this AZ has hit its per-AZ instance cap (balanced mode only).

    Judged against instances actually held (READ FROM AWS this round) — so a
    restart correctly skips an AZ that is already full and keeps topping up the
    ones that aren't.
    """
    if args.per_az_count is None:
        return False
    return held.get(az, 0) >= args.per_az_count


def sweep_once(client, args, azs, offered, held, made):
    """One full pass over the AZs. Mutates held/made in place.

    Grabs at most ONE instance per AZ per pass; the --watch loop repeats sweeps
    to accumulate toward the target (so capacity that trickles out gets caught).
    """
    throttle_attempt = 0
    for az in azs:
        if sum(held.values()) >= args.target_count:
            return
        if _az_full(args, held, az):
            continue  # balanced mode: this AZ already at its per-AZ cap
        if az not in offered:
            continue  # g7e.48xlarge not offered in this AZ — skip
        try:
            resp = reserve_one(client, az, not args.live, args.end_hours)
            crid = resp["CapacityReservation"]["CapacityReservationId"]
            _on_grab(args, crid, az, held, made)
            throttle_attempt = 0
        except ClientError as e:
            kind = classify(e)
            if kind == "dryrun_ok":
                log.info("[dry-run] would reserve %s @ %s (+1 instance)",
                         INSTANCE_TYPE, az)
                _on_grab(args, "(dry-run)", az, held, made)
            elif kind == "capacity":
                log.info("no capacity: %s @ %s — next", INSTANCE_TYPE, az)
            elif kind == "throttle":
                log.warning("throttled, backing off (attempt %d)", throttle_attempt)
                backoff_sleep(throttle_attempt)
                throttle_attempt += 1
            else:
                log.error("FATAL on %s @ %s: %s", INSTANCE_TYPE, az,
                          e.response["Error"])
                raise


def run(args):
    client = ec2_client(args.region)

    if args.list:
        # Targets are read automatically from grabs.jsonl inside print_list,
        # so plain --list shows progress. If the caller DID pass them, prefer
        # those: target_count defaults to DEFAULT_TARGET (placeholder) — treat
        # that as unset; if only --per-az-count given, derive total = per_az x
        # number-of --azs.
        tgt = None if args.target_count == DEFAULT_TARGET else args.target_count
        per_az = args.per_az_count
        if per_az and tgt is None and args.azs:
            tgt = per_az * len(args.azs)
        if tgt is None and per_az is None:
            print_list(client)                       # auto-read from ledger
        else:
            print_list(client, target_count=tgt, per_az_count=per_az)
        return

    if args.cancel_all:
        cancel_all(client, not args.live)
        return

    log.info("region=%s type=%s dry_run=%s target_count=%d end_hours=%s watch=%s",
             args.region, INSTANCE_TYPE, not args.live, args.target_count,
             args.end_hours, args.watch)

    all_azs = list_azs(client)
    offered = offered_in_azs(client)

    # Lock the sweep to --azs if given, else use every AZ in the region.
    # ODCR needs NO subnet, so any available AZ works.
    azs, missing = resolve_azs(all_azs, args.azs)
    if missing:
        log.warning("requested AZs not present in %s (ignored): %s",
                    args.region, missing)
    if not azs:
        log.error("no usable AZs after applying --azs %s — nothing to do", args.azs)
        return
    not_offered = [az for az in azs if az not in offered]
    if not_offered:
        log.warning("%s not offered in these target AZs (will skip): %s",
                    INSTANCE_TYPE, not_offered)
    log.info("target AZs: %s", azs)

    # BALANCED mode: if --per-az-count is set and --target-count was left at the
    # default, auto-compute the total as per_az_count * number-of-AZs so the
    # caller only has to supply ONE number.
    if args.per_az_count is not None:
        auto_total = args.per_az_count * len(azs)
        if args.target_count == DEFAULT_TARGET:  # untouched default
            args.target_count = auto_total
            log.info("balanced mode: per-az cap %d x %d AZ -> target %d instances",
                     args.per_az_count, len(azs), args.target_count)
        elif args.target_count != auto_total:
            log.warning("balanced mode: --target-count %d != per-az %d x %d AZ "
                        "(%d); using --target-count as the hard overall stop",
                        args.target_count, args.per_az_count, len(azs), auto_total)

    made = []  # reservations created in THIS process (for end-of-run listing)

    # held = per-AZ instances we ACTUALLY hold, the stop-gate's source of truth.
    # LIVE: seed from AWS so a restart resumes exactly where we left off.
    # dry-run: start empty and simulate locally so the plan preview is clean.
    #
    # IMPORTANT: count ONLY the AZs we're sweeping (only_azs=set(azs)). If you
    # target one AZ (--azs us-east-1d) but already hold stock in another
    # (us-east-1b), that out-of-scope stock would inflate the TOTAL gate and
    # stop the run before the targeted AZ is filled.
    held = held_count_by_az(client, only_azs=set(azs)) if args.live else {}
    if args.live and held:
        log.info("resumed from AWS: %d instance(s) already held in target AZs %s",
                 sum(held.values()), held)

    if args.watch:
        log.info("WATCH mode: re-sweeping every %ds until %d instance(s) reserved "
                 "(Ctrl-C to stop)", args.interval, args.target_count)
        rounds = 0
        while sum(held.values()) < args.target_count:
            rounds += 1
            # Re-read AWS truth each round (live): this is what makes the watch
            # loop self-correcting and restart-safe — per-AZ caps are always
            # judged against what we really hold right now. Same AZ-scope filter
            # as the seed above.
            if args.live:
                held = held_count_by_az(client, only_azs=set(azs))
            log.info("--- watch round %d (have %d/%d instances | per-AZ %s) ---",
                     rounds, sum(held.values()), args.target_count, held)
            sweep_once(client, args, azs, offered, held, made)
            if sum(held.values()) >= args.target_count:
                break
            time.sleep(args.interval)
        log.info("WATCH target reached after %d round(s)", rounds)
    else:
        sweep_once(client, args, azs, offered, held, made)

    log.info("=== DONE: holding %d/%d instance(s) (this run created %d reservation(s)) ===",
             sum(held.values()), args.target_count, len(made))
    if args.per_az_count is not None:
        for az in azs:
            got = held.get(az, 0)
            flag = "FULL" if got >= args.per_az_count else "short"
            log.info("  per-AZ %s: %d/%d instance(s) [%s]",
                     az, got, args.per_az_count, flag)
    for crid, t, a in made:
        log.info("  %s %s @ %s", crid, t, a)
    if not args.live:
        log.info("(dry-run — nothing was actually reserved, no billing)")
    else:
        log.warning("LIVE reservations are billing NOW at On-Demand rate.")
        log.warning("Run: python3 grab_g7e_odcr.py --cancel-all --live  to stop.")


def main():
    p = argparse.ArgumentParser(
        description="Grab g7e.48xlarge via On-Demand Capacity Reservations "
                    "(count-based)")
    p.add_argument("--region", default=DEFAULT_REGION,
                   help="AWS region to target (default %s)" % DEFAULT_REGION)
    p.add_argument("--target-count", type=int, default=DEFAULT_TARGET,
                   help="stop once this many g7e.48xlarge instances are held "
                        "(default %d). If --per-az-count is set and this is left "
                        "at default, the total is auto-computed as "
                        "per-az-count x number-of-AZs." % DEFAULT_TARGET)
    p.add_argument("--per-az-count", type=int, default=None,
                   help="BALANCED mode: cap EACH AZ at this many instances. The "
                        "cap is checked against instances actually held in that "
                        "AZ (read from AWS each round), so the sweep skips any AZ "
                        "already at its cap and keeps hunting the rest — "
                        "reservations stay even across --azs AND a restart "
                        "resumes per-AZ correctly. e.g. --azs us-east-1b "
                        "us-east-1d --per-az-count 2 caps each AZ at 2 instances, "
                        "4 total.")
    p.add_argument("--azs", nargs="*",
                   help="lock to these AZ names, e.g. --azs us-east-1b us-east-1d "
                        "(default: every AZ in the region)")
    p.add_argument("--end-hours", type=float, default=None,
                   help="auto-expire reservations after N hours (billing guard)")
    p.add_argument("--watch", action="store_true",
                   help="loop forever, re-sweeping until target reached (24x7 hunt)")
    p.add_argument("--interval", type=int, default=60,
                   help="seconds between sweeps in --watch mode (default 60)")
    p.add_argument("--live", action="store_true",
                   help="actually reserve (default is dry-run)")
    p.add_argument("--cancel-all", action="store_true",
                   help="cancel all reservations tagged %s=%s" % (TAG_KEY, TAG_VAL))
    p.add_argument("--list", action="store_true",
                   help="list current reservations + per-AZ/total instance "
                        "summary (auto-reads target from grabs.jsonl), then exit")
    args = p.parse_args()
    try:
        run(args)
    except KeyboardInterrupt:
        log.info("interrupted — stopping watch")
    except ClientError as e:
        log.error("AWS error: %s", e.response.get("Error"))
        sys.exit(1)


if __name__ == "__main__":
    main()
