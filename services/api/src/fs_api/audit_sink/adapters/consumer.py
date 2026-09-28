"""Kafka consumer loop for the audit sink: poll a batch, append to Postgres, THEN commit offsets (at-least-once)."""
from __future__ import annotations

import logging
import time

from confluent_kafka import Consumer, KafkaError
from fscore.contracts import AUDIT_EVENTS

from ..sink import AuditSink

log = logging.getLogger("fs_api.audit_sink")


def make_consumer(bootstrap: str, group: str = "audit-sink") -> Consumer:
    return Consumer({
        "bootstrap.servers": bootstrap, "group.id": group, "enable.auto.commit": False, "auto.offset.reset": "earliest",
        "isolation.level": "read_committed", "max.poll.interval.ms": 300_000,
    })


def run(consumer: Consumer, sink: AuditSink, stop=lambda: False, batch: int = 200, poll_s: float = 1.0) -> None:
    consumer.subscribe([AUDIT_EVENTS])
    backoff = 0.5
    while not stop():
        msgs = [m for m in consumer.consume(num_messages=batch, timeout=poll_s) if m.error() is None or m.error().code() != KafkaError._PARTITION_EOF]
        good = [m for m in msgs if m.error() is None]
        if not good:
            continue
        try:
            outcome = sink.handle([m.value() for m in good])
        except Exception:
            log.exception("audit write failed, will retry the same batch")
            time.sleep(backoff)
            backoff = min(backoff * 2, 10.0)
            consumer.seek_all(good) if hasattr(consumer, "seek_all") else _rewind(consumer, good)
            continue
        backoff = 0.5
        consumer.commit(message=good[-1], asynchronous=False)
        log.info("audit batch", extra={"received": outcome.received, "inserted": outcome.inserted, "poison": outcome.poison})
    consumer.close()


def _rewind(consumer: Consumer, msgs: list) -> None:
    from confluent_kafka import TopicPartition

    first: dict[tuple[str, int], int] = {}
    for m in msgs:
        first.setdefault((m.topic(), m.partition()), m.offset())
    for (t, p), off in first.items():
        consumer.seek(TopicPartition(t, p, off))
