"""confluent-kafka wiring for the processor lanes (integration-tested; excluded from the gate)."""
from __future__ import annotations

import structlog
from confluent_kafka import Consumer, Producer

from ..application.bulk_service import BulkRunner, BulkService
from ..application.priority_service import PriorityService
from ..models import InMsg, OutMsg

log = structlog.get_logger("fs_processor.kafka")


def to_in(m) -> InMsg:
    hdrs = {k: (v if isinstance(v, bytes) else str(v).encode()) for k, v in (m.headers() or [])}
    ts = m.timestamp()[1] if m.timestamp() and m.timestamp()[1] > 0 else 0
    return InMsg(m.topic(), m.partition(), m.offset(), m.key(), m.value(), hdrs, ts)


class KafkaSink:
    """Idempotent producer: acks=all, zstd. flush() returns undelivered count."""

    def __init__(self, bootstrap: str) -> None:
        self._p = Producer({
            "bootstrap.servers": bootstrap, "enable.idempotence": True, "acks": "all",
            "compression.type": "zstd", "linger.ms": 5,
        })
        self._errors = 0

    def _cb(self, err, _msg) -> None:
        if err is not None:
            self._errors += 1
            log.error("delivery_failed", error=str(err))

    def produce(self, msg: OutMsg) -> None:
        while True:
            try:
                self._p.produce(msg.topic, key=msg.key, value=msg.value,
                                headers=list(msg.headers.items()), on_delivery=self._cb)
                return
            except BufferError:
                self._p.poll(0.05)

    def flush(self, timeout_s: float = 30.0) -> int:
        left = self._p.flush(timeout_s)
        errs, self._errors = self._errors, 0
        return left + errs


def make_consumer(bootstrap: str, group_id: str, topics: list[str]) -> Consumer:
    c = Consumer({
        "bootstrap.servers": bootstrap, "group.id": group_id, "enable.auto.commit": False,
        "auto.offset.reset": "earliest", "partition.assignment.strategy": "cooperative-sticky",
    })
    c.subscribe(topics, on_assign=_on_assign, on_revoke=_on_revoke)
    return c


def _on_assign(_consumer, partitions) -> None:
    log.info("partitions_assigned", partitions=[(p.topic, p.partition, p.offset) for p in partitions])


def _on_revoke(_consumer, partitions) -> None:
    log.info("partitions_revoked", partitions=[(p.topic, p.partition) for p in partitions])


__all__ = ["BulkRunner", "BulkService", "KafkaSink", "PriorityService", "make_consumer", "to_in"]
