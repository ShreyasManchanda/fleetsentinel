"""Create MinIO buckets host-side (boto3): lake + WORM audit-anchor. Idempotent.

Replaces the old minio/mc container (image left Docker Hub; quay.io 401s). Run:
uv run --frozen python tools/minio_init.py [--endpoint http://localhost:9000]
"""
from __future__ import annotations

import argparse
import os
import sys

import structlog

log = structlog.get_logger("minio_init")
LAKE = "fleetsentinel-lake"
ANCHORS = "fleetsentinel-audit-anchor"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=os.getenv("S3_ENDPOINT", "http://localhost:9000"))
    ap.add_argument("--user", default=os.getenv("MINIO_ROOT_USER", "minioadmin"))
    ap.add_argument("--password", default=os.getenv("MINIO_ROOT_PASSWORD", "minioadmin"))
    args = ap.parse_args()
    import boto3
    from botocore.exceptions import ClientError

    s3 = boto3.client("s3", endpoint_url=args.endpoint,
                      aws_access_key_id=args.user, aws_secret_access_key=args.password,
                      region_name="us-east-1")
    existing = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
    if LAKE not in existing:
        s3.create_bucket(Bucket=LAKE)
    try:
        s3.put_bucket_lifecycle_configuration(
            Bucket=LAKE,
            LifecycleConfiguration={"Rules": [{"ID": "expire-raw-400d", "Status": "Enabled",
                                               "Filter": {"Prefix": ""},
                                               "Expiration": {"Days": 400}}]})
    except ClientError as e:
        log.warning("lifecycle_skipped", error=str(e))
    if ANCHORS not in existing:
        s3.create_bucket(Bucket=ANCHORS, ObjectLockEnabledForBucket=True)
    try:
        s3.put_object_lock_configuration(
            Bucket=ANCHORS,
            ObjectLockConfiguration={"ObjectLockEnabled": "Enabled",
                                      "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": 1}}})
    except ClientError as e:
        log.warning("objectlock_skipped", error=str(e))
    log.info("buckets_ready", lake=LAKE, anchors=ANCHORS, endpoint=args.endpoint)
    return 0


if __name__ == "__main__":
    sys.exit(main())
