"""Kafka producers: audit.events.v1 (Protobuf, key = tenant) and config.mappings.v1 (compacted JSON, key = OEM)."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from confluent_kafka import KafkaException, Producer
from fscore.contracts import AUDIT_EVENTS, CONFIG_MAPPINGS

from ..domain.audit import AuditRecord, to_proto


def make_producer(bootstrap: str, client_id: str = "fs-api") -> Producer:
    return Producer({
        "bootstrap.servers": bootstrap, "client.id": client_id, "enable.idempotence": True, "acks": "all",
        "linger.ms": 5, "compression.type": "zstd", "message.timeout.ms": 10_000,
    })


class KafkaAuditPublisher:
    """Non-blocking produce (librdkafka batches in a background thread). Delivery failures surface via the callback and are
    counted; the ResilientAuditPublisher wrapper handles synchronous queue-full / broker-down errors by buffering."""

    def __init__(self, producer: Producer, topic: str = AUDIT_EVENTS):
        self._p, self._topic = producer, topic
        self.delivery_errors = 0

    def _on_delivery(self, err: Any, _msg: Any) -> None:
        if err is not None:
            self.delivery_errors += 1

    async def publish(self, rec: AuditRecord) -> None:
        try:
            self._p.produce(self._topic, key=(rec.tenant_id or "unknown").encode(), value=to_proto(rec).SerializeToString(),
                            on_delivery=self._on_delivery)
            self._p.poll(0)
        except (BufferError, KafkaException) as e:
            raise ConnectionError(f"audit produce failed: {e}") from e

    async def ping(self) -> dict[str, Any]:
        md = await asyncio.to_thread(self._p.list_topics, self._topic, 2.0)
        if self._topic not in md.topics or md.topics[self._topic].error is not None:
            raise ConnectionError(f"topic {self._topic} unavailable")
        return {"brokers": len(md.brokers)}


class KafkaMappingPublisher:
    def __init__(self, producer: Producer, topic: str = CONFIG_MAPPINGS):
        self._p, self._topic = producer, topic

    async def publish(self, oem: str, document: dict[str, Any]) -> None:
        err: list[Any] = []
        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def cb(e: Any, _m: Any) -> None:
            if e is not None:
                err.append(e)
            loop.call_soon_threadsafe(done.set)

        self._p.produce(self._topic, key=oem.encode(), value=json.dumps(document, separators=(",", ":")).encode(), on_delivery=cb)
        await asyncio.to_thread(self._p.flush, 5.0)
        await asyncio.wait_for(done.wait(), 6.0)
        if err:
            raise ConnectionError(f"mapping publish failed: {err[0]}")
