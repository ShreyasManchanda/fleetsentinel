"""Lake sink service: buffer per (partition-group) -> flush offset-named Parquet objects.

Idempotency: the object key derives from (topic, partition, start/end offsets), so flushing
the same offset range twice (e.g. after a crash before the Kafka commit) PUTs the same key
with identical bytes — an atomic replace, never a duplicate.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

import structlog
from fleetsentinel.v1 import telemetry_pb2

from ..domain.batch import events_to_parquet_bytes
from ..domain.naming import key_for_event
from ..metrics import LakeSinkMetrics
from ..models import InMsg

log = structlog.get_logger("fs_lake_sink.service")


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...


@dataclass(slots=True)
class _Buffer:
    events: list[telemetry_pb2.TelemetryEvent] = field(default_factory=list)
    start_offset: int = -1
    end_offset: int = -1
    topic: str = ""
    partition: int = 0


class LakeSinkService:
    def __init__(
        self,
        store: ObjectStore,
        max_records: int = 1000,
        metrics: LakeSinkMetrics | None = None,
    ) -> None:
        if max_records < 1:
            raise ValueError("max_records >= 1")
        self.store = store
        self.max_records = max_records
        self.metrics = metrics or LakeSinkMetrics()
        self._bufs: dict[int, _Buffer] = {}
        self.written_keys: list[str] = []

    def add(self, msg: InMsg) -> str | None:
        """Buffer one Kafka record; returns the object key when a flush was triggered."""
        if not msg.value:
            return None
        ev = telemetry_pb2.TelemetryEvent()
        ev.ParseFromString(msg.value)
        buf = self._bufs.get(msg.partition)
        if buf is None:
            buf = self._bufs[msg.partition] = _Buffer(
                topic=msg.topic, partition=msg.partition, start_offset=msg.offset)
        buf.events.append(ev)
        buf.end_offset = msg.offset
        self.metrics.records.inc()
        if len(buf.events) >= self.max_records:
            return self.flush(msg.partition)
        return None

    def add_batch(self, msgs: list[InMsg]) -> list[str]:
        t0 = time.perf_counter()
        keys: list[str] = []
        for m in msgs:
            key = self.add(m)
            if key is not None:
                keys.append(key)
        log.info("lake_batch", inputs=len(msgs), objects=len(keys))
        self.metrics.batch_seconds.observe(time.perf_counter() - t0)
        return keys

    def flush(self, partition: int) -> str | None:
        buf = self._bufs.get(partition)
        if not buf or not buf.events:
            return None
        first = buf.events[0]
        key = key_for_event(buf.topic, buf.partition, buf.start_offset, buf.end_offset,
                            first.ts_ms, first.oem, first.vin)
        data = events_to_parquet_bytes(buf.events)
        self.store.put(key, data, content_type="application/parquet")
        self.metrics.objects.inc()
        self.metrics.object_bytes.inc(len(data))
        self.written_keys.append(key)
        log.info("lake_object", key=key, records=len(buf.events), bytes=len(data))
        del self._bufs[partition]
        return key

    def flush_all(self) -> list[str]:
        keys: list[str] = []
        for p in sorted(self._bufs):
            key = self.flush(p)
            if key is not None:
                keys.append(key)
        return keys

    def pending(self, partition: int) -> int:
        buf = self._bufs.get(partition)
        return len(buf.events) if buf else 0
