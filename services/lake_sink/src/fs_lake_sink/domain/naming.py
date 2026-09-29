"""Deterministic object naming: Hive prefix + offset-derived file name.

Key layout::

    date={YYYY-MM-DD}/oem={OEM}/vehicle_shard={N}/{topic}-{partition:05d}-{start:012d}-{end:012d}.parquet

The name derives only from (topic, partition, start/end offsets, event date/oem/shard),
so a re-write after a crash replaces an identical object (atomic S3/MinIO PUT semantics:
readers never see partial files). No rename step — renames do not exist on object stores.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fscore.hashing import vin_shard


def event_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC).strftime("%Y-%m-%d")


def hive_prefix(date: str, oem: str, shard: int) -> str:
    return f"date={date}/oem={oem}/vehicle_shard={shard}"


def object_key(
    topic: str,
    partition: int,
    start_offset: int,
    end_offset: int,
    date: str,
    oem: str,
    shard: int,
) -> str:
    name = f"{topic}-{partition:05d}-{start_offset:012d}-{end_offset:012d}.parquet"
    return f"{hive_prefix(date, oem, shard)}/{name}"


def key_for_event(topic: str, partition: int, start_offset: int, end_offset: int,
                  ts_ms: int, oem: str, vin: str) -> str:
    return object_key(topic, partition, start_offset, end_offset, event_date(ts_ms), oem, vin_shard(vin))
