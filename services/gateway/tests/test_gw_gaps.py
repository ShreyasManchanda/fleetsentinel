"""Gateway gap tests: envelope edges, spill accounting/replay determinism, pipeline
delivery branches not covered by test_gw_domain/test_gw_pipeline (DLQ truncation,
traceparent sampling, batch-id fallback, partial drain_spill)."""
from __future__ import annotations

import pytest
from fs_gateway.application.pipeline import GatewayPipeline, Item
from fs_gateway.domain.dedup import BatchDeduper, batch_key
from fs_gateway.domain.envelope import EnvelopeError, parse_topic, validate
from fs_gateway.domain.spill import SpillBuffer, SpillRecord, encode
from fs_gateway.metrics import GatewayMetrics
from fscore import contracts as C
from fscore.sketches import RotatingBloom

OEMS = frozenset({"OEM_A", "OEM_B", "OEM_C", "OEM_D"})


class MemStore:
    def __init__(self):
        self.keys = set()
        self.nx_calls = self.add_calls = 0

    def add_many(self, keys, ttl_s):
        self.add_calls += 1
        self.keys.update(keys)

    def set_nx_many(self, keys, ttl_s):
        self.nx_calls += 1
        out = []
        for k in keys:
            out.append(k not in self.keys)
            self.keys.add(k)
        return out


class FakeSink:
    def __init__(self):
        self.down = False
        self.delivered = []
        self.fail_each = None

    async def send_many(self, msgs):
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
    p = GatewayPipeline(sink, deduper, sp, OEMS, metrics, queue_size=queue_size, batch_size=50,
                        batch_wait_s=0.001, retry_backoff_s=0.001, rand=lambda: 0.5, **kw)
    return p, sink, sp, metrics


def item(topic="telemetry/OEM_A/3", payload=b'{"a":1}', acks=None, **props):
    return Item(topic, payload, 1234567, (lambda: acks.append(payload)) if acks is not None else None, props)


# ---------------------------------------------------------------- envelope edges
def test_shard_upper_boundary_and_extra_segments():
    assert parse_topic(f"telemetry/OEM_A/{C.N_SHARDS - 1}")[2] == C.N_SHARDS - 1
    with pytest.raises(EnvelopeError) as e:
        parse_topic(f"telemetry/OEM_A/{C.N_SHARDS}")
    assert e.value.reason == "BAD_SHARD"
    with pytest.raises(EnvelopeError) as e2:
        parse_topic("telemetry/OEM_A/1/extra")
    assert e2.value.reason == "BAD_TOPIC"
    with pytest.raises(EnvelopeError) as e3:
        validate("telemetry//1", b"x", OEMS)
    assert e3.value.reason == "UNKNOWN_OEM"


# ---------------------------------------------------------------- spill accounting
def _rec(i: int) -> SpillRecord:
    return SpillRecord("ingest.raw.v1", b"k", bytes([i]) * 20, {"n": str(i).encode()})


def test_pending_bytes_drops_after_partial_ack(tmp_path):
    sb = SpillBuffer(tmp_path, max_bytes=10_000_000, segment_bytes=10_000)
    for i in range(6):
        sb.append(_rec(i))
    before = sb.pending_bytes
    got = sb.peek(2)
    sb.ack(got[-1][0], 2)
    assert sb.pending_bytes < before and len(sb) == 4


def test_ack_of_full_non_write_segment_advances_read_seg(tmp_path):
    sb = SpillBuffer(tmp_path, max_bytes=10_000_000, segment_bytes=200)
    for i in range(12):
        sb.append(_rec(i))
    assert len(sb._segs) > 1
    first_seg = sb._read_seg
    peeked = sb.peek(1000)
    # ack everything in the first segment only
    in_first = [c for c, _ in peeked if c[0] == first_seg]
    sb.ack(in_first[-1], len(in_first))
    assert sb._read_seg > first_seg or first_seg not in sb._segs
    rest = [int(r.headers["n"]) for _, r in sb.peek(100)]
    assert rest == sorted(rest) and len(rest) == 12 - len(in_first)


def test_reopen_after_ack_replays_remaining_in_order(tmp_path):
    sb = SpillBuffer(tmp_path, segment_bytes=10_000)
    for i in range(5):
        sb.append(_rec(i))
    got = sb.peek(2)
    sb.ack(got[-1][0], 2)
    sb.close()
    sb2 = SpillBuffer(tmp_path, segment_bytes=10_000)
    assert [int(r.headers["n"]) for _, r in sb2.peek(10)] == [2, 3, 4]
    assert len(sb2) == 3  # count survives the restart


def test_encode_roundtrip_with_empty_headers():
    rec = SpillRecord("t", b"key", b"value")
    raw = encode(rec)
    import struct as _struct

    ml, vl, _crc = _struct.unpack(">III", raw[:12])
    assert ml + vl == len(raw) - 12


def test_spill_len_never_negative_on_over_ack(tmp_path):
    sb = SpillBuffer(tmp_path)
    sb.append(_rec(0))
    got = sb.peek(1)
    sb.ack(got[-1][0], 5)  # caller claims more than pending
    assert len(sb) == 0


# ---------------------------------------------------------------- dedup edges
def test_deduper_empty_input_is_noop():
    store = MemStore()
    d = BatchDeduper(store, RotatingBloom(100, 0.01), 60, cold_exact_s=0, metrics=GatewayMetrics())
    assert d.filter([]) == []
    assert store.add_calls == 0 and store.nx_calls == 0


# ---------------------------------------------------------------- pipeline branches
@pytest.mark.asyncio
async def test_oversized_payload_dlq_has_empty_value(tmp_path):
    p, sink, _, _ = make(tmp_path, max_payload_bytes=10)
    acks: list = []
    await p.process([item("telemetry/OEM_A/1", b"x" * 500, acks)])
    assert len(acks) == 1 and len(sink.delivered) == 1
    dlq = sink.delivered[0]
    assert dlq.topic == C.DLQ_INGEST and dlq.value == b""  # oversized raw is not retained
    assert b"TOO_LARGE" in dlq.headers["error_detail"]


@pytest.mark.asyncio
async def test_bulk_traceparent_sampled_when_rand_hits(tmp_path):
    metrics = GatewayMetrics()
    sink = FakeSink()
    deduper = BatchDeduper(MemStore(), RotatingBloom(1000, 0.01), 60, cold_exact_s=0, metrics=metrics)
    p = GatewayPipeline(sink, deduper, None, OEMS, metrics, batch_size=50, batch_wait_s=0.001,
                        retry_backoff_s=0.001, rand=lambda: 0.0)  # always below the 1% sample rate
    await p.process([item("telemetry/OEM_A/3", b"bulk-sampled")])
    assert sink.delivered[0].headers[C.H_TRACEPARENT].endswith(b"-01")


@pytest.mark.asyncio
async def test_batch_id_falls_back_to_dedup_key(tmp_path):
    p, sink, _, _ = make(tmp_path)
    await p.process([item("telemetry/OEM_A/3", b"no-props-batch")])
    rec = sink.delivered[0]
    assert rec.headers[C.H_BATCH_ID].decode() == batch_key("OEM_A", "telemetry", b"no-props-batch")


@pytest.mark.asyncio
async def test_drain_spill_partial_success_acks_prefix_and_stays_spilling(tmp_path):
    p, sink, sp, _ = make(tmp_path)
    acks: list = []
    sink.down = True
    for i in range(4):
        await p.process([item(payload=f"k{i}".encode(), acks=acks)])
    assert len(sp) == 4
    sink.down = False
    sink.fail_each = {1, 2, 3}  # only the first replayed record succeeds
    assert await p.drain_spill() is False
    assert len(sp) == 3 and p._spilling
    assert sink.delivered[0].value == b"k0"


@pytest.mark.asyncio
async def test_drain_spill_total_failure_keeps_backlog(tmp_path):
    p, sink, sp, _ = make(tmp_path)
    sink.down = True
    await p.process([item(payload=b"held")])
    assert await p.drain_spill() is False  # sink still down
    assert len(sp) == 1 and p._spilling


@pytest.mark.asyncio
async def test_event_topic_without_traceprop_gets_generated_traceparent(tmp_path):
    p, sink, _, _ = make(tmp_path)
    it = Item("event/OEM_B/9", b"prio", 123, None, {})
    await p.process([it])
    assert sink.delivered[0].headers[C.H_TRACEPARENT].startswith(b"00-")
