"""confluent-kafka consumer wiring (integration-tested; excluded from the gate)."""
from __future__ import annotations

import structlog
from confluent_kafka import Consumer

from ..application.sink import LakeSinkService
from ..models import InMsg

log = structlog.get_logger("fs_lake_sink.kafka")


def to_in(m) -> InMsg:
    hdrs = {k: (v if isinstance(v, bytes) else str(v).encode()) for k, v in (m.headers() or [])}
    ts = m.timestamp()[1] if m.timestamp() and m.timestamp()[1] > 0 else 0
    return InMsg(m.topic(), m.partition(), m.offset(), m.key(), m.value(), hdrs, ts)


def run(service: LakeSinkService, bootstrap: str, group_id: str, topic: str, batch_max: int,
        poll_timeout_s: float, stop=lambda: False) -> None:
    consumer = Consumer({
        "bootstrap.servers": bootstrap, "group.id": group_id, "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
    })
    consumer.subscribe([topic])
    try:
        while not stop():
            batch = consumer.consume(num_messages=batch_max, timeout=poll_timeout_s)
            msgs = [m for m in batch if m.error() is None]
            if msgs:
                service.add_batch([to_in(m) for m in msgs])
                service.flush_all()
                consumer.commit(asynchronous=False)
    finally:
        consumer.close()


__all__ = ["LakeSinkService", "run", "to_in"]
