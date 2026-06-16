#!/usr/bin/env python3
"""Unit tests for g7e common.py — every shared helper.

All mocked, no AWS, no real sleeping, no polluting the repo's logs/ dir.

Run:  python3 -m unittest test_common -v
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from botocore.exceptions import ClientError

import common
from common import (
    DEFAULT_REGION, INSTANCE_TYPE,
    resolve_azs, list_azs, offered_in_azs,
    classify, backoff_sleep, record_grab, setup_logging, ec2_client,
)


def _err(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Op")


class ResolveAzs(unittest.TestCase):
    ALL = ["us-east-1a", "us-east-1b", "us-east-1c", "us-east-1d"]

    def test_none_returns_all(self):
        self.assertEqual(resolve_azs(self.ALL, None), (list(self.ALL), []))

    def test_empty_returns_all(self):
        self.assertEqual(resolve_azs(self.ALL, []), (list(self.ALL), []))

    def test_filters_to_requested(self):
        sel, missing = resolve_azs(self.ALL, ["us-east-1b", "us-east-1d"])
        self.assertEqual(sel, ["us-east-1b", "us-east-1d"])
        self.assertEqual(missing, [])

    def test_reports_missing(self):
        sel, missing = resolve_azs(self.ALL, ["us-east-1b", "us-east-1z"])
        self.assertEqual(sel, ["us-east-1b"])
        self.assertEqual(missing, ["us-east-1z"])

    def test_preserves_requested_order(self):
        sel, _ = resolve_azs(self.ALL, ["us-east-1d", "us-east-1b"])
        self.assertEqual(sel, ["us-east-1d", "us-east-1b"])  # not re-sorted


class ListAzs(unittest.TestCase):
    def test_returns_sorted_available_zone_names(self):
        client = mock.Mock()
        client.describe_availability_zones.return_value = {
            "AvailabilityZones": [
                {"ZoneName": "us-east-1d"},
                {"ZoneName": "us-east-1a"},
                {"ZoneName": "us-east-1c"},
            ]
        }
        self.assertEqual(list_azs(client),
                         ["us-east-1a", "us-east-1c", "us-east-1d"])
        # must filter on state=available
        _, kwargs = client.describe_availability_zones.call_args
        self.assertEqual(kwargs["Filters"],
                         [{"Name": "state", "Values": ["available"]}])


class OfferedInAzs(unittest.TestCase):
    def test_builds_az_set_for_the_single_type(self):
        client = mock.Mock()
        client.describe_instance_type_offerings.return_value = {
            "InstanceTypeOfferings": [
                {"InstanceType": INSTANCE_TYPE, "Location": "us-east-1b"},
                {"InstanceType": INSTANCE_TYPE, "Location": "us-east-1d"},
            ]
        }
        self.assertEqual(offered_in_azs(client), {"us-east-1b", "us-east-1d"})
        # must filter the offerings query to our one instance type
        _, kwargs = client.describe_instance_type_offerings.call_args
        self.assertEqual(kwargs["LocationType"], "availability-zone")
        self.assertEqual(kwargs["Filters"],
                         [{"Name": "instance-type", "Values": [INSTANCE_TYPE]}])

    def test_empty_offerings(self):
        client = mock.Mock()
        client.describe_instance_type_offerings.return_value = {
            "InstanceTypeOfferings": []}
        self.assertEqual(offered_in_azs(client), set())


class Classify(unittest.TestCase):
    def test_dryrun(self):
        self.assertEqual(classify(_err("DryRunOperation")), "dryrun_ok")

    def test_capacity_variants(self):
        for code in ("InsufficientInstanceCapacity", "InsufficientCapacity",
                     "Unsupported", "InsufficientHostCapacity"):
            self.assertEqual(classify(_err(code)), "capacity", code)

    def test_throttle_variants(self):
        for code in ("RequestLimitExceeded", "Throttling", "ThrottlingException"):
            self.assertEqual(classify(_err(code)), "throttle", code)

    def test_unknown_is_fatal(self):
        self.assertEqual(classify(_err("UnauthorizedOperation")), "fatal")

    def test_missing_code_is_fatal(self):
        e = ClientError({"Error": {}}, "Op")
        self.assertEqual(classify(e), "fatal")


class BackoffSleep(unittest.TestCase):
    def test_delay_grows_then_caps(self):
        seen = []
        # random.uniform(0, d) -> return d so we can assert the upper bound
        with mock.patch.object(common.time, "sleep", lambda s: seen.append(s)), \
             mock.patch.object(common.random, "uniform", lambda a, b: b):
            for attempt in range(8):
                backoff_sleep(attempt, base=1.0, cap=20.0)
        # 1,2,4,8,16, then capped at 20,20,20
        self.assertEqual(seen[:5], [1, 2, 4, 8, 16])
        self.assertTrue(all(s <= 20.0 for s in seen))
        self.assertEqual(seen[5:], [20.0, 20.0, 20.0])

    def test_jitter_within_zero_and_delay(self):
        captured = {}
        with mock.patch.object(common.time, "sleep", lambda s: None), \
             mock.patch.object(common.random, "uniform",
                               lambda a, b: captured.update(lo=a, hi=b)):
            backoff_sleep(3, base=1.0, cap=20.0)  # delay = 8
        self.assertEqual(captured, {"lo": 0, "hi": 8})


class RecordGrab(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ledger = os.path.join(self.tmp, "grabs.jsonl")
        self._p1 = mock.patch.object(common, "LOGS_DIR", self.tmp)
        self._p2 = mock.patch.object(common, "GRAB_LEDGER", self.ledger)
        self._p1.start()
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()

    def test_dry_run_writes_nothing(self):
        record_grab("odcr", "us-east-1b", 1, 1, 4, "us-east-1", dry_run=True)
        self.assertFalse(os.path.exists(self.ledger))

    def test_live_appends_one_json_line_with_count_fields(self):
        record_grab("odcr", "us-east-1b", 1, 2, 4, "us-east-1", dry_run=False,
                    per_az_count=2, per_az_total=1)
        with open(self.ledger) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec["via"], "odcr")
        self.assertEqual(rec["instance_type"], INSTANCE_TYPE)
        self.assertEqual(rec["az"], "us-east-1b")
        self.assertEqual(rec["count"], 1)
        self.assertEqual(rec["total_count"], 2)
        self.assertEqual(rec["target_count"], 4)
        self.assertEqual(rec["region"], "us-east-1")
        self.assertEqual(rec["per_az_count"], 2)    # balanced-mode cap recorded
        self.assertEqual(rec["per_az_total"], 1)    # instances held in this AZ
        self.assertIn("ts", rec)

    def test_per_az_fields_null_when_not_balanced(self):
        # no per-az args -> fields present but null (not missing)
        record_grab("odcr", "us-east-1b", 1, 1, 3, "us-east-1", dry_run=False)
        with open(self.ledger) as f:
            rec = json.loads(f.read().splitlines()[0])
        self.assertIsNone(rec["per_az_count"])
        self.assertIsNone(rec["per_az_total"])

    def test_appends_not_overwrites(self):
        for i in range(3):
            record_grab("odcr", "us-east-1b", 1, i + 1, 10, "us-east-1",
                        dry_run=False)
        with open(self.ledger) as f:
            self.assertEqual(len(f.read().splitlines()), 3)


class SetupLogging(unittest.TestCase):
    def test_console_only_when_no_file(self):
        logger = setup_logging(None)
        self.assertEqual(logger.name, "g7e-grab")
        from logging.handlers import RotatingFileHandler
        self.assertEqual(len(logger.handlers), 1)
        self.assertFalse(any(isinstance(h, RotatingFileHandler)
                             for h in logger.handlers))

    def test_adds_rotating_file_handler(self):
        tmp = tempfile.mkdtemp()
        from logging.handlers import RotatingFileHandler
        with mock.patch.object(common, "LOGS_DIR", tmp):
            logger = setup_logging("t.log")
        self.assertTrue(any(isinstance(h, RotatingFileHandler)
                            for h in logger.handlers))
        self.assertTrue(os.path.exists(os.path.join(tmp, "t.log")))

    def test_idempotent_no_handler_pileup(self):
        a = setup_logging(None)
        n1 = len(a.handlers)
        b = setup_logging(None)
        self.assertEqual(len(b.handlers), n1)
        self.assertIs(a, b)  # same named logger


class Ec2Client(unittest.TestCase):
    def test_passes_region(self):
        with mock.patch.object(common.boto3, "client") as mk:
            ec2_client("us-west-2")
            mk.assert_called_once_with("ec2", region_name="us-west-2")

    def test_default_region(self):
        with mock.patch.object(common.boto3, "client") as mk:
            ec2_client()
            mk.assert_called_once_with("ec2", region_name=DEFAULT_REGION)


if __name__ == "__main__":
    unittest.main()
