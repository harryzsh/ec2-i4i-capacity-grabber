"""G/VT On-Demand vCPU quota preflight for the g7e.48xlarge grabber.

g7e belongs to the EC2 **G** family, so the relevant Service Quota is
"Running On-Demand G and VT instances" (quota code L-DB2E81BA), measured in
**vCPUs** — NOT instance count. Each g7e.48xlarge is 192 vCPU, so to hold N
instances you need a quota of at least 192 x N.

This module is the small, testable core behind `grab_g7e_odcr.py --check-quota`.
The functions take an injected boto3 service-quotas client so they can be unit
tested with a mock (no AWS calls).

NOTE: a quota increase is approved ASYNCHRONOUSLY by AWS and a sufficient quota
does NOT guarantee capacity — you still have to grab an ODCR. See 配额.md.
"""
import boto3

from common import DEFAULT_REGION, INSTANCE_TYPE, VCPU_PER

# Service Quotas identifiers for the G/VT On-Demand vCPU limit.
SERVICE_CODE = "ec2"
G_VT_QUOTA_CODE = "L-DB2E81BA"
G_VT_QUOTA_NAME = "Running On-Demand G and VT instances"


def service_quotas_client(region=DEFAULT_REGION):
    """A boto3 service-quotas client (separate API from EC2)."""
    return boto3.client("service-quotas", region_name=region)


def vcpus_needed(count):
    """vCPUs required to run `count` g7e.48xlarge instances (192 each)."""
    return count * VCPU_PER


def max_instances_for(vcpu_quota):
    """How many whole g7e.48xlarge a given vCPU quota allows."""
    return int(vcpu_quota // VCPU_PER)


def get_g_vt_quota(client):
    """Current APPLIED G/VT On-Demand vCPU quota value (float).

    Reads the live applied value via GetServiceQuota. Pending increase
    requests are not reflected here (use the Service Quotas console /
    request-history to see those).
    """
    resp = client.get_service_quota(
        ServiceCode=SERVICE_CODE, QuotaCode=G_VT_QUOTA_CODE)
    return resp["Quota"]["Value"]


def check_quota(client, target_count):
    """Preflight the G/VT quota against a desired instance target.

    Returns a dict:
      current_vcpu   - applied G/VT vCPU quota
      needed_vcpu    - 192 * target_count
      max_instances  - how many g7e.48xlarge the current quota allows
      target_count   - echoed input
      sufficient     - True if current_vcpu >= needed_vcpu
    """
    current = get_g_vt_quota(client)
    needed = vcpus_needed(target_count)
    return {
        "instance_type": INSTANCE_TYPE,
        "quota_code": G_VT_QUOTA_CODE,
        "quota_name": G_VT_QUOTA_NAME,
        "current_vcpu": current,
        "needed_vcpu": needed,
        "max_instances": max_instances_for(current),
        "target_count": target_count,
        "sufficient": current >= needed,
    }
