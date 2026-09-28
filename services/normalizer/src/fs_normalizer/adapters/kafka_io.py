"""confluent-kafka wiring for the normaliser (integration-tested; excluded from the unit-coverage gate)."""
from __future__ import annotations

import time
import uuid

import structlog
from confluent_kafka import Consumer, KafkaError, Producer

from ..models import InMsg, OutMsg
from ..runner import Runner

log = structlog.get_logger("fs_normalizer.kafka")


def to_in(m) -> InMsg:
    hdrs = {k: (v if isinstance(v, bytes) else str(v).encode()) for k, v in (m.headers() or [])}
    ts = m.timestamp()[1] if m.timestamp() and m.timestamp()[1] > 0 else 0
    return InMsg(m.topic(), m.partition(), m.offset(), m.key(), m.value(), hdrs, ts)


class KafkaSink:
    """Idempotent producer: acks=all, zstd, small linger. flush() returns the number of undelivered messages."""

    def __init__(self, bootstrap: str) -> None:
        self._p = Producer({
            "bootstrap.servers": bootstrap, "enable.idempotence": True, "acks": "all",
            "compression.type": "zstd", "linger.ms": 5, "batch.num.messages": 10000,
            "queue.buffering.max.messages": 500000, "queue.buffering.max.kbytes": 262144,
        })
        self._errors = 0

    def _cb(self, err, _msg) -> None:
        if err is not None:
            self._errors += 1
            log.error("delivery_failed", error=str(err))

    def produce(self, msg: OutMsg) -> None:
        while True:
            try:
                self._p.produce(msg.topic, key=msg.key, value=msg.value, headers=list(msg.headers.items()),
                                on_delivery=self._cb)
                return
            except BufferError:
                self._p.poll(0.05)

    def flush(self, timeout_s: float = 30.0) -> int:
        left = self._p.flush(timeout_s)
        errs, self._errors = self._errors, 0
        return left + errs


def run(runner: Runner, bootstrap: str, group_id: str, in_topic: str, config_topic: str, batch_max: int,
        poll_timeout_s: float, stop=lambda: False) -> None:
    consumer = Consumer({
        "bootstrap.servers": bootstrap, "group.id": group_id, "enable.auto.commit": False,
        "auto.offset.reset": "earliest", "partition.assignment.strategy": "cooperative-sticky",
        "fetch.min.bytes": 1, "max.partition.fetch.bytes": 4194304,
    })
    cfg = Consumer({
        "bootstrap.servers": bootstrap, "group.id": f"{group_id}-cfg-{uuid.uuid4().hex[:8]}",
        "enable.auto.commit": False, "auto.offset.reset": "earliest",
    })
    cfg.subscribe([config_topic])
    _catch_up_config(runner, cfg)
    consumer.subscribe([in_topic])
    try:
        while not stop():
            for m in cfg.consume(num_messages=100, timeout=0):
                if m.error() is None:
                    runner.handle_config([to_in(m)])
            batch = consumer.consume(num_messages=batch_max, timeout=poll_timeout_s)
            msgs = [m for m in batch if m.error() is None]
            for m in batch:
                if m.error() is not None and m.error().code() != KafkaError._PARTITION_EOF:
                    log.error("consume_error", error=str(m.error()))
            if msgs:
                runner.handle_batch([to_in(m) for m in msgs])
                consumer.commit(asynchronous=False)
    finally:
        consumer.close()
        cfg.close()


def _catch_up_config(runner: Runner, cfg: Consumer, quiet_s: float = 1.5, max_s: float = 15.0) -> None:
    """Read the compacted config topic until quiet so the first data batch already sees the newest mappings."""
    start = last = time.monotonic()
    while time.monotonic() - last < quiet_s and time.monotonic() - start < max_s:
        got = [m for m in cfg.consume(num_messages=100, timeout=0.5) if m.error() is None]
        if got:
            runner.handle_config([to_in(m) for m in got])
            last = time.monotonic()