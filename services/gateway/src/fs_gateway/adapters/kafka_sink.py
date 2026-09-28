"""confluent-kafka idempotent producer as an async Sink (integration-tested; excluded from the unit-coverage gate)."""
from __future__ import annotations

import asyncio
import time

import structlog
from confluent_kafka import KafkaException, Producer

from ..domain.spill import SpillRecord

log = structlog.get_logger("fs_gateway.kafka")


class KafkaSink:
    """enable.idempotence, acks=all, zstd, linger ~5 ms. A tiny circuit breaker makes an outage fail fast into the spill."""

    def __init__(self, bootstrap: str, down_backoff_s: float = 2.0) -> None:
        self._p = Producer({
            "bootstrap.servers": bootstrap, "enable.idempotence": True, "acks": "all", "compression.type": "zstd",
            "linger.ms": 5, "batch.num.messages": 5000, "max.in.flight.requests.per.connection": 5,
            "queue.buffering.max.messages": 200000, "queue.buffering.max.kbytes": 524288,
            "request.timeout.ms": 5000, "delivery.timeout.ms": 10000, "message.max.bytes": 2097152,
        })
        self._down_until = 0.0
        self._backoff = down_backoff_s
        self._poller: asyncio.Task | None = None

    async def start(self) -> None:
        self._poller = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        while True:
            self._p.poll(0)
            await asyncio.sleep(0.005)

    async def send_many(self, msgs: list[SpillRecord]) -> list[bool]:
        if time.monotonic() < self._down_until:
            return [False] * len(msgs)
        loop = asyncio.get_running_loop()
        futs: list[asyncio.Future[bool]] = []
        for m in msgs:
            fut: asyncio.Future[bool] = loop.create_future()
            futs.append(fut)

            def cb(err, _msg, fut=fut) -> None:
                if not fut.done():
                    fut.set_result(err is None)

            for _attempt in range(20):
                try:
                    self._p.produce(m.topic, key=m.key, value=m.value, headers=list(m.headers.items()), on_delivery=cb)
                    break
                except BufferError:
                    self._p.poll(0.01)
                except KafkaException as e:
                    log.error("produce_failed", error=str(e))
                    fut.set_result(False)
                    break
            else:
                fut.set_result(False)
        self._p.poll(0)
        try:
            res = await asyncio.wait_for(asyncio.gather(*futs), timeout=30)
        except TimeoutError:
            res = [f.result() if f.done() else False for f in futs]
        if not all(res):
            self._down_until = time.monotonic() + self._backoff
        return list(res)

    async def close(self) -> None:
        if self._poller:
            self._poller.cancel()
        self._p.flush(5)