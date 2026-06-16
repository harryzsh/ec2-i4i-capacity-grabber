"""Shared helpers for the g7e.48xlarge capacity-grab script.

grab_g7e_odcr.py imports from here.
Region is configurable via --region (default: us-east-1).

DESIGN NOTE — this is the COUNT-BASED sibling of the repo's i4i grabber.
The i4i version counts vCPU (cores); this one counts INSTANCES (台数). There
is exactly one instance type (g7e.48xlarge), so there is no vCPU table and no
large-first type sorting — every unit is "one g7e.48xlarge".
"""
import os
import json
import time
import random
import logging
import datetime
from logging.handlers import RotatingFileHandler

import boto3
from botocore.exceptions import ClientError

DEFAULT_REGION = "us-east-1"

# The ONLY instance type this grabber reserves. Single-type by requirement —
# no size fallback. VCPU_PER is informational only (logging/quota math); the
# stop-gates count INSTANCES, not cores.
INSTANCE_TYPE = "g7e.48xlarge"
VCPU_PER = 192

# Tag stamped on every reservation we create, so --list / --cancel-all /
# held_count_by_az can find exactly ours. Distinct from the i4i grabber's tag
# so the two never collide in the same account.
TAG_KEY = "purpose"
TAG_VAL = "g7e-grab"

# All logs/ledgers live next to the scripts, regardless of cwd.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
GRAB_LEDGER = os.path.join(LOGS_DIR, "grabs.jsonl")

# Errors that just mean "no capacity here, move on" — NOT a script failure.
CAPACITY_ERRORS = {
    "InsufficientInstanceCapacity",
    "InsufficientCapacity",
    "Unsupported",  # type not offered in this AZ
    "InsufficientHostCapacity",
}
# Throttling → back off and retry the SAME target.
THROTTLE_ERRORS = {"RequestLimitExceeded", "Throttling", "ThrottlingException"}
# DryRun "success" sentinel.
DRYRUN_OK = "DryRunOperation"


def setup_logging(logfile=None):
    """Console + (optional) rotating file logger.

    logfile: base name like 'grab_g7e_odcr.log'. Written under logs/ with
             rotation (5 MB x 5 backups) so it never fills the disk.
    """
    logger = logging.getLogger("g7e-grab")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    # console
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    # rotating file
    if logfile:
        os.makedirs(LOGS_DIR, exist_ok=True)
        fh = RotatingFileHandler(
            os.path.join(LOGS_DIR, logfile),
            maxBytes=5 * 1024 * 1024, backupCount=5,
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


def record_grab(via, az, count, total_count, target_count, region, dry_run,
                per_az_count=None, per_az_total=None):
    """Append one JSON line to the ledger every time we secure capacity.

    Machine-readable feed for downstream tooling (parsers, dashboards, etc.).
    Skipped during dry-run so the ledger only ever holds real grabs.

    count:        instances secured by THIS grab (always 1 — one ODCR per call).
    total_count:  total instances held across all in-scope AZs after this grab.
    target_count: the overall instance target we're grabbing toward.
    per_az_count: the --per-az-count cap in effect (None if not balanced mode).
    per_az_total: instances held in THIS az after this grab (so the ledger
                  shows how a balanced run progresses per AZ, not just total).
    """
    if dry_run:
        return
    os.makedirs(LOGS_DIR, exist_ok=True)
    rec = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "via": via,                       # "odcr"
        "instance_type": INSTANCE_TYPE,
        "az": az,
        "region": region,
        "count": count,                   # instances this grab (1)
        "total_count": total_count,       # total instances held after this grab
        "target_count": target_count,     # overall instance target
        "per_az_count": per_az_count,     # cap per AZ (null = not balanced mode)
        "per_az_total": per_az_total,     # instances held in this AZ after grab
    }
    with open(GRAB_LEDGER, "a") as f:
        f.write(json.dumps(rec) + "\n")


def ec2_client(region=DEFAULT_REGION):
    return boto3.client("ec2", region_name=region)


def resolve_azs(all_azs, requested):
    """Lock the sweep to a caller-supplied AZ list.

    all_azs:   AZ names actually available in the region (from list_azs).
    requested: AZ names passed via --azs (e.g. ['us-east-1b','us-east-1d']);
               None/empty means "use every available AZ".
    Returns (selected_azs, missing) where missing are requested AZs that don't
    exist in the region (so the caller can warn instead of silently dropping).
    """
    if not requested:
        return list(all_azs), []
    avail = set(all_azs)
    selected = [az for az in requested if az in avail]
    missing = [az for az in requested if az not in avail]
    return selected, missing


def list_azs(client):
    """All available AZs in the region."""
    resp = client.describe_availability_zones(
        Filters=[{"Name": "state", "Values": ["available"]}]
    )
    return sorted(z["ZoneName"] for z in resp["AvailabilityZones"])


def offered_in_azs(client):
    """The set of AZ names where g7e.48xlarge is actually offered.

    Single-type version of the i4i offered_types_by_az: we only ever reserve
    INSTANCE_TYPE, so we just need to know which AZs can hold it and skip the
    rest (avoids guaranteed-fail CreateCapacityReservation calls).
    """
    resp = client.describe_instance_type_offerings(
        LocationType="availability-zone",
        Filters=[{"Name": "instance-type", "Values": [INSTANCE_TYPE]}],
    )
    return {o["Location"] for o in resp["InstanceTypeOfferings"]}


def backoff_sleep(attempt, base=1.0, cap=20.0):
    """Exponential backoff with full jitter."""
    delay = min(cap, base * (2 ** attempt))
    time.sleep(random.uniform(0, delay))


def classify(err: ClientError):
    """Return one of: 'dryrun_ok', 'capacity', 'throttle', 'fatal'."""
    code = err.response.get("Error", {}).get("Code", "")
    if code == DRYRUN_OK:
        return "dryrun_ok"
    if code in CAPACITY_ERRORS:
        return "capacity"
    if code in THROTTLE_ERRORS:
        return "throttle"
    return "fatal"
