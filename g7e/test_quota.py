#!/usr/bin/env python3
"""Unit tests for quota.py — G/VT vCPU quota preflight.

All mocked, no AWS. Run:  python3 -m unittest test_quota -v
"""
import unittest
from unittest import mock

import quota
from quota import (
    SERVICE_CODE, G_VT_QUOTA_CODE, G_VT_QUOTA_NAME,
    service_quotas_client, vcpus_needed, max_instances_for,
    get_g_vt_quota, check_quota,
)
from common import INSTANCE_TYPE, VCPU_PER, DEFAULT_REGION


def _fake_client(value):
    """A mock service-quotas client whose GetServiceQuota returns `value`."""
    client = mock.Mock()
    client.get_service_quota.return_value = {"Quota": {"Value": value}}
    return client


class Constants(unittest.TestCase):
    def test_g_vt_quota_code_and_service(self):
        self.assertEqual(SERVICE_CODE, "ec2")
        self.assertEqual(G_VT_QUOTA_CODE, "L-DB2E81BA")
        self.assertEqual(G_VT_QUOTA_NAME, "Running On-Demand G and VT instances")

    def test_vcpu_per_is_192(self):
        # g7e.48xlarge = 192 vCPU; the whole quota math hinges on this.
        self.assertEqual(VCPU_PER, 192)


class VcpusNeeded(unittest.TestCase):
    def test_scales_by_192(self):
        self.assertEqual(vcpus_needed(0), 0)
        self.assertEqual(vcpus_needed(1), 192)
        self.assertEqual(vcpus_needed(4), 768)


class MaxInstancesFor(unittest.TestCase):
    def test_floor_division(self):
        self.assertEqual(max_instances_for(768), 4)
        self.assertEqual(max_instances_for(0), 0)

    def test_rounds_down_partial(self):
        # 800 vCPU only fits 4 whole instances (4*192=768), not 5.
        self.assertEqual(max_instances_for(800), 4)

    def test_below_one_instance(self):
        self.assertEqual(max_instances_for(100), 0)


class GetGVtQuota(unittest.TestCase):
    def test_reads_value_with_correct_codes(self):
        client = _fake_client(768.0)
        self.assertEqual(get_g_vt_quota(client), 768.0)
        client.get_service_quota.assert_called_once_with(
            ServiceCode="ec2", QuotaCode="L-DB2E81BA")


class CheckQuota(unittest.TestCase):
    def test_sufficient_when_quota_meets_need(self):
        client = _fake_client(768.0)            # 4 instances worth
        r = check_quota(client, target_count=4)
        self.assertTrue(r["sufficient"])
        self.assertEqual(r["current_vcpu"], 768.0)
        self.assertEqual(r["needed_vcpu"], 768)
        self.assertEqual(r["max_instances"], 4)
        self.assertEqual(r["target_count"], 4)
        self.assertEqual(r["instance_type"], INSTANCE_TYPE)
        self.assertEqual(r["quota_code"], G_VT_QUOTA_CODE)

    def test_insufficient_when_quota_below_need(self):
        client = _fake_client(384.0)            # only 2 instances worth
        r = check_quota(client, target_count=4)
        self.assertFalse(r["sufficient"])
        self.assertEqual(r["max_instances"], 2)
        self.assertEqual(r["needed_vcpu"], 768)

    def test_exact_boundary_is_sufficient(self):
        client = _fake_client(192.0)
        r = check_quota(client, target_count=1)
        self.assertTrue(r["sufficient"])        # current == needed -> OK

    def test_zero_quota_is_insufficient_for_any_target(self):
        client = _fake_client(0.0)              # common default for new GPU
        r = check_quota(client, target_count=1)
        self.assertFalse(r["sufficient"])
        self.assertEqual(r["max_instances"], 0)


class ServiceQuotasClient(unittest.TestCase):
    def test_passes_region(self):
        with mock.patch.object(quota.boto3, "client") as mk:
            service_quotas_client("us-west-2")
            mk.assert_called_once_with("service-quotas", region_name="us-west-2")

    def test_default_region(self):
        with mock.patch.object(quota.boto3, "client") as mk:
            service_quotas_client()
            mk.assert_called_once_with("service-quotas", region_name=DEFAULT_REGION)


if __name__ == "__main__":
    unittest.main()
