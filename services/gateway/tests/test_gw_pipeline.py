import asyncio

import pytest
from fs_gateway.application.pipeline import GatewayPipeline, Item, make_traceparent
from fs_gateway.domain.dedup import BatchDeduper
from fs_gateway.domain.spill import SpillBuffer
from fs_gateway.metrics import GatewayMetrics
from fscore import contracts as C
from fscore.sketches import RotatingBloom

OEMS = frozenset({"OEM_A", "OEM_B", "OEM_C", "OEM_D"})


class MemStore:
    def __init__(self):
        self.keys = set()

    def add_many(self, keys, ttl_s):
        self.keys.update(keys)

    def set_nx_many(self, keys, ttl_s):
        out = []
        for k in keys:
            out.append(k not in self.keys)
            self.keys.add(k)
        return out


class FakeSink:
    def __init__(self):
        self.down = False
        self.delivered = []
        self.calls = 0
        self.fail_each = None            # set of indexes (per call) to fail

    async def send_many(self, msgs):
        self.calls += 1
        if self.down:
            return [False] * len(msgs)
        res = []
        for i, m in enumerate(msgs):
            if self.fail_each and i in self.fail_each:
                res.append(False)
            else:
                self.delivered.append(m)
                res.append(True)
        return res


def make(tmp_path=None, sink=None, spill=True, queue_size=100, **kw):
    metrics = GatewayMetrics()
    sink = sink or FakeSink()
    deduper = BatchDeduper(MemStore(), RotatingBloom(1000, 0.01), 60, cold_exact_s=0, metrics=metrics)
    sp = SpillBuffer(tmp_path / "spill", **kw.pop("spill_kw", {})) if spill else None
    p = GatewayPipeline(sink, deduper, sp, OEMS, metrics, queue_size=queue_size, batch_size=50, batch_wait_s=0.001,
                        retry_backoff_s=0.001, rand=lambda: 0.5, **kw)
    return p, sink, sp, metrics


def item(topic="telemetry/OEM_A/3", payload=b'{"a":1}', acks=None, **props):
    return Item(topic, payload, 1234567, (lambda: acks.append(payload)) if acks is not None else None, props)


def test_traceparent_shape():
    assert make_traceparent(True).endswith("-01") and make_traceparent(False).endswith("-00")
    assert len(make_traceparent(True).split("-")[1]) == 32


@pytest.mark.asyncio
async def test_routing_headers_and_ack_after_delivery(tmp_path):
    p, sink, _, m = make(tmp_path)
    acks: list = []
    await p.process([item("telemetry/OEM_A/3", b"bulk", acks), item("event/OEM_B/9", b"prio", acks, batch_id="conn-77")])
    assert acks == [b"bulk", b"prio"]
    bulk, prio = sink.delivered
    assert (bulk.topic, bulk.key) == (C.INGEST_RAW, b"OEM_A:3") and (prio.topic, prio.key) == (C.INGEST_PRIORITY, b"OEM_B:9")
    assert bulk.headers[C.H_OEM] == b"OEM_A" and bulk.headers[C.H_RECEIVED_TS_MS] == b"1234567"
    assert prio.headers[C.H_BATCH_ID] == b"conn-77" and len(bulk.headers[C.H_BATCH_ID]) == 32
    assert prio.headers[C.H_TRACEPARENT].endswith(b"-01")             # priority is always sampled
    assert bulk.headers[C.H_TRACEPARENT].endswith(b"-00")             # rand=0.5 > 1% => not sampled
    assert m.produced.labels(C.INGEST_RAW)._value.get() == 1


@pytest.mark.asyncio
async def test_bad_envelopes_go_to_dlq_and_are_acked(tmp_path):
    p, sink, _, m = make(tmp_path)
    acks: list = []
    await p.process([item("telemetry/OEM_Z/1", b"x", acks), item("bogus", b"y", acks), item("telemetry/OEM_A/1", b"", acks)])
    assert len(acks) == 3 and len(sink.delivered) == 3
    assert all(r.topic == C.DLQ_INGEST and r.headers[C.H_ERROR_REASON] == b"BAD_ENVELOPE" for r in sink.delivered)
    assert sink.delivered[0].key == b"UNKNOWN" and b"UNKNOWN_OEM" in sink.delivered[0].headers["error_detail"]
    assert m.rejected.labels("UNKNOWN_OEM")._value.get() == 1


@pytest.mark.asyncio
async def test_duplicate_batch_dropped_but_acked(tmp_path):
    p, sink, _, m = make(tmp_path)
    acks: list = []
    await p.process([item(payload=b"same", acks=acks)])
    await p.process([item(payload=b"same", acks=acks), item(payload=b"other", acks=acks)])
    assert len(sink.delivered) == 2 and len(acks) == 3
    assert m.duplicates._value.get() == 1


@pytest.mark.asyncio
async def test_kafka_outage_spills_then_replays_in_order(tmp_path):
    p, sink, sp, m = make(tmp_path)
    acks: list = []
    sink.down = True
    for i in range(5):
        await p.process([item(payload=f"p{i}".encode(), acks=acks)])
    assert len(sp) == 5 and len(acks) == 5 and not sink.delivered      # acked because durably spilled
    sink.down = False
    await p.process([item(payload=b"p5", acks=acks)])                  # new batch goes BEHIND the backlog
    assert [r.value for r in sink.delivered] == [f"p{i}".encode() for i in range(6)]
    assert len(sp) == 0 and not p._spilling
    # p5 arrived while the backlog existed, so it was spilled BEHIND p0..p4
    # (order preservation) and then replayed with them: 6 spilled, 6 replayed.
    assert m.spilled._value.get() == 6 and m.replayed._value.get() == 6


@pytest.mark.asyncio
async def test_partial_failure_spills_only_failed(tmp_path):
    p, sink, sp, _ = make(tmp_path)
    sink.fail_each = {1}
    acks: list = []
    await p.process([item(payload=b"a", acks=acks), item(payload=b"b", acks=acks), item(payload=b"c", acks=acks)])
    assert [r.value for r in sink.delivered] == [b"a", b"c"] and len(sp) == 1 and len(acks) == 3
    sink.fail_each = None
    assert await p.drain_spill()
    assert sink.delivered[-1].value == b"b"


@pytest.mark.asyncio
async def test_spill_full_means_no_ack_backpressure(tmp_path):
    p, sink, sp, _ = make(tmp_path, spill_kw={"max_bytes": 600})
    sink.down = True
    acks: list = []
    # NOTE: payloads must be DISTINCT per batch: the dedup key is derived from
    # (oem, kind, payload), so identical payloads would collapse into 1 batch
    # + 5 duplicates and never fill the spill buffer.
    items = [item(payload=f"{i:06d}".encode() + b"x" * 244, acks=acks) for i in range(6)]
    task = asyncio.create_task(p.process(items))
    await asyncio.sleep(0.1)
    assert not task.done() and len(acks) < 6                          # blocked: cannot ack what it cannot hold
    sink.down = False
    await asyncio.wait_for(task, 5)
    assert len(acks) == 6 and len(sink.delivered) + len(sp) == 6


@pytest.mark.asyncio
async def test_no_spill_configured_retries_until_kafka_returns():
    p, sink, _, _ = make(spill=False, tmp_path=None)
    sink.down = True
    acks: list = []
    task = asyncio.create_task(p.process([item(payload=b"q", acks=acks)]))
    await asyncio.sleep(0.05)
    assert not acks
    sink.down = False
    await asyncio.wait_for(task, 5)
    assert acks == [b"q"]


@pytest.mark.asyncio
async def test_backpressure_blocks_reader_and_counts_seconds(tmp_path):
    p, _, _, m = make(tmp_path, queue_size=2)
    await p.submit(item(payload=b"1"))
    await p.submit(item(payload=b"2"))
    assert p.queue.qsize() == 2 and m.queue_depth._value.get() == 2
    blocked = asyncio.create_task(p.submit(item(payload=b"3")))
    await asyncio.sleep(0.15)
    assert not blocked.done()                                          # reader is stopped while the queue is full
    await p.queue.get()
    await asyncio.wait_for(blocked, 2)
    assert m.backpressure_seconds._value.get() >= 0.1


@pytest.mark.asyncio
async def test_run_loop_end_to_end_with_batching_and_stop(tmp_path):
    p, sink, sp, _ = make(tmp_path)
    acks: list = []
    # NOTE: the run loop must be started BEFORE submitting: submit() blocks
    # when the bounded queue (size 100 here) is full, so submitting all 120
    # items first deadlocks. This also matches production (reader+worker run
    # concurrently).
    task = asyncio.create_task(p.run())
    for i in range(120):
        await p.submit(item(payload=f"m{i}".encode(), acks=acks))
    for _ in range(200):
        if len(acks) == 120:
            break
        await asyncio.sleep(0.02)
    assert len(acks) == 120 and len(sink.delivered) == 120
    p.stop()
    await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_run_loop_drains_leftover_spill_when_idle(tmp_path):
    p, sink, sp, _ = make(tmp_path)
    sink.down = True
    await p.process([item(payload=b"z")])
    sink.down = False
    task = asyncio.create_task(p.run())
    for _ in range(200):
        if sink.delivered:
            break
        await asyncio.sleep(0.02)
    p.stop()
    await asyncio.wait_for(task, 3)
    assert [r.value for r in sink.delivered] == [b"z"]