"""MinIO/S3 object store via the S3 API (integration-tested; excluded from the gate)."""
from __future__ import annotations

import structlog

log = structlog.get_logger("fs_lake_sink.s3")


class S3Store:
    """Atomic PUTs to a versioned-or-plain bucket; identical keys overwrite identically."""

    def __init__(self, endpoint_url: str, bucket: str, prefix: str = "lake/") -> None:
        import boto3

        self._s3 = boto3.client("s3", endpoint_url=endpoint_url)
        self._bucket = bucket
        self._prefix = prefix

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._s3.put_object(Bucket=self._bucket, Key=f"{self._prefix}{key}", Body=data,
                            ContentType=content_type)
        log.info("s3_put", key=key, bytes=len(data))
