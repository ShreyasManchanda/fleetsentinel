"""Gateway pipeline: bounded queue (back-pressure) -> envelope check -> batch dedup -> Kafka, with disk spill on outage.

Acknowledgement contract: an MQTT message is acknowledged (``item.ack``) only AFTER its batch was delivered to Kafka
(acks=all, idempotent producer) or durably spilled to disk (fsync). If neither is possible the worker retries with
backoff and does not acknowledge, so the queue fills, the MQTT reader blocks, and Mosquitto pushes back on the OEM.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import structlog
from fscore import contracts as C

from ..domain.dedup import BatchDeduper, batch_key
from ..domain.envelope import Envelope, EnvelopeError, validate
from ..domain.spill import SpillBuffer, SpillFull, SpillRecord
from ..metrics import GatewayMetrics

log = structlog.get_logger("fs_gateway.pipeline")


@dataclass(slots=True)
class Item:
    topic: str
    payload: bytes
    received_ts_ms: int
    ack: Callable[[], None] | None = None
    props: dict[str, str] = field(default_factory=dict)      # MQTT v5 user properties (batch_id, traceparent)


class Sink(Protocol):
    async def send_many(self, msgs: list[SpillRecord]) -> list[bool]:
        """Deliver messages; return per-message success. Must not raise for broker-side failures."""


def make_traceparent(sampled: bool) -> str:
    return f"00-{os.urandom(16).hex()}-{os.urandom(8).hex()}-{'01' if sampled else '00'}"


class GatewayPipeline:
    def __init__(self, sink: Sink, deduper: BatchDeduper, spill: SpillBuffer | None, known_oems: frozenset[str],
                 metrics: GatewayMetrics | None = None, queue_size: int = 10_000, batch_size: int = 200,
                 batch_wait_s: float = 0.02, max_payload_bytes: int = 1_048_576, sample_bulk: float = 0.01,
                 retry_backoff_s: float = 0.5, rand: Callable[[], float] | None = None) -> None:
        self.sink, self.deduper, self.spill = sink, deduper, spill
        self.known_oems = known_oems
        self.metrics = metrics or GatewayMetrics()
        self.queue: asyncio.Queue[Item] = asyncio.Queue(maxsize=queue_size)
        self.batch_size, self.batch_wait_s = batch_size, batch_wait_s
        self.max_payload_bytes, self.sample_bulk = max_payload_bytes, sample_bulk
        self.retry_backoff_s = retry_backoff_s
        self._rand = rand or (lambda: int.from_bytes(os.urandom(2), "big") / 65536)
        self._spilling = bool(spill and len(spill))
        self._stop = False

    # ------------------------------------------------------------------ producer side (MQTT reader)
    async def submit(self, item: Item) -> None:
        """Enqueue; when the queue is full this blocks (the reader stops reading/acking) and the wait is measured."""
        if self.queue.full():
            t0 = time.monotonic()
            await self.queue.put(item)
            self.metrics.backpressure_seconds.inc(time.monotonic() - t0)
        else:
            self.queue.put_nowait(item)
        self.metrics.queue_depth.set(self.queue.qsize())

    # ------------------------------------------------------------------ worker
    async def run(self) -> None:
        while not self._stop:
            batch = await self._next_batch()
            if batch:
                await self.process(batch)
            elif self._spilling:
                await self.drain_spill()

    def stop(self) -> None:
        self._stop = True

    async def _next_batch(self) -> list[Item]:
        try:
            first = await asyncio.wait_for(self.queue.get(), timeout=1.0)
        except TimeoutError:
            return []
        batch = [first]
        deadline = time.monotonic() + self.batch_wait_s
        while len(batch) < self.batch_size:
            try:
                batch.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self.queue.get(), timeout=left))
                except TimeoutError:
                    break
        self.metrics.queue_depth.set(self.queue.qsize())
        return batch

    async def process(self, items: list[Item]) -> None:
        t0 = time.perf_counter()
        good: list[tuple[Item, Envelope]] = []
        out: list[tuple[SpillRecord, list[Item]]] = []
        for it in items:
            try:
                good.append((it, validate(it.topic, it.payload, self.known_oems, self.max_payload_bytes)))
                self.metrics.received.labels(good[-1][1].kind).inc()
            except EnvelopeError as e:
                self.metrics.rejected.labels(e.reason).inc()
                out.append((self._dlq_record(it, e), [it]))
        keys = [batch_key(env.oem, env.kind, it.payload) for it, env in good]
        keep = self.deduper.filter(keys) if keys else []
        dup_items: list[Item] = []
        for (it, env), key, k in zip(good, keys, keep, strict=True):
            if not k:
                dup_items.append(it)
                continue
            out.append((self._record(it, env, key), [it]))
        await self._deliver(out)
        for it in dup_items:            # duplicates are acknowledged (already delivered earlier)
            self._ack(it)
        if out or dup_items:
            log.info("gateway_batch", received=len(items), produced=len(out), duplicates=len(dup_items),
                     spilling=self._spilling, queue_depth=self.queue.qsize())
        self.metrics.batch_seconds.observe(time.perf_counter() - t0)

    # ------------------------------------------------------------------ delivery + spill
    def _record(self, it: Item, env: Envelope, key: str) -> SpillRecord:
        topic = C.INGEST_RAW if env.kind == "telemetry" else C.INGEST_PRIORITY
        tp = it.props.get(C.H_TRACEPARENT) or make_traceparent(env.kind == "event" or self._rand() < self.sample_bulk)
        headers = {C.H_OEM: env.oem.encode(), C.H_BATCH_ID: (it.props.get(C.H_BATCH_ID) or key).encode(),
                   C.H_RECEIVED_TS_MS: str(it.received_ts_ms).encode(), C.H_TRACEPARENT: tp.encode()}
        return SpillRecord(topic, env.kafka_key, it.payload, headers)

    def _dlq_record(self, it: Item, e: EnvelopeError) -> SpillRecord:
        parts = it.topic.split("/")
        oem = parts[1] if len(parts) > 1 and parts[1] in self.known_oems else "UNKNOWN"
        value = it.payload if len(it.payload) <= self.max_payload_bytes else b""
        headers = {C.H_OEM: oem.encode(), C.H_ERROR_REASON: C.ERR_BAD_ENVELOPE.encode(),
                   "error_detail": f"{e.reason}: {e.detail}"[:200].encode(),
                   C.H_RECEIVED_TS_MS: str(it.received_ts_ms).encode(), "mqtt_topic": it.topic[:120].encode()}
        return SpillRecord(C.DLQ_INGEST, oem.encode(), value, headers)

    def _ack(self, it: Item) -> None:
        if it.ack is not None:
            it.ack()

    async def _deliver(self, out: list[tuple[SpillRecord, list[Item]]]) -> None:
        if not out:
            return
        if self._spilling:                       # keep order: everything queues behind the spilled backlog
            await self._spill_all(out)
            await self.drain_spill()
            return
        results = await self.sink.send_many([r for r, _ in out])
        failed: list[tuple[SpillRecord, list[Item]]] = []
        for (rec, its), ok in zip(out, results, strict=True):
            if ok:
                self.metrics.produced.labels(rec.topic).inc()
                for it in its:
                    self._ack(it)
            else:
                failed.append((rec, its))
        if failed:
            log.warning("kafka_unavailable_spilling", failed=len(failed))
            await self._spill_all(failed)

    async def _spill_all(self, recs: list[tuple[SpillRecord, list[Item]]]) -> None:
        if self.spill is None:                   # no spill configured: retry until the sink accepts (back-pressure)
            pending = recs
            while pending and not self._stop:
                await asyncio.sleep(self.retry_backoff_s)
                res = await self.sink.send_many([r for r, _ in pending])
                nxt = []
                for (rec, its), ok in zip(pending, res, strict=True):
                    if ok:
                        self.metrics.produced.labels(rec.topic).inc()
                        for it in its:
                            self._ack(it)
                    else:
                        nxt.append((rec, its))
                pending = nxt
            return
        self._spilling = True
        i = 0
        while i < len(recs) and not self._stop:
            try:
                self.spill.append(recs[i][0])
            except SpillFull:
                log.error("spill_full_backpressure", pending=len(self.spill), bytes=self.spill.pending_bytes)
                self.spill.sync()
                for _, its in recs[:i]:
                    for it in its:
                        self._ack(it)
                recs = recs[i:]
                i = 0
                await self.drain_spill()
                await asyncio.sleep(self.retry_backoff_s)
                continue
            self.metrics.spilled.inc()
            i += 1
        self._spilling = True
        self.spill.sync()
        self.metrics.spill_bytes.set(self.spill.pending_bytes)
        for _, its in recs[:i]:
            for it in its:
                self._ack(it)

    async def drain_spill(self, max_batches: int = 50) -> bool:
        """Replay the spilled backlog to Kafka in order. Returns True when the buffer is empty."""
        if self.spill is None:
            return True
        for _ in range(max_batches):
            pending = self.spill.peek(self.batch_size)
            if not pending:
                self._spilling = False
                self.metrics.spill_bytes.set(self.spill.pending_bytes)
                return True
            res = await self.sink.send_many([r for _, r in pending])
            n_ok = 0
            for ok in res:
                if not ok:
                    break
                n_ok += 1
            if n_ok:
                self.spill.ack(pending[n_ok - 1][0], n_ok)
                self.metrics.replayed.inc(n_ok)
                for _, rec in pending[:n_ok]:
                    self.metrics.produced.labels(rec.topic).inc()
            if n_ok < len(pending):
                self.metrics.spill_bytes.set(self.spill.pending_bytes)
                await asyncio.sleep(self.retry_backoff_s)
                return False
        return False