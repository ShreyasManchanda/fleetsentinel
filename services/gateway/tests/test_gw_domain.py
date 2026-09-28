import os

import pytest
from fs_gateway.domain.dedup import BatchDeduper, batch_key
from fs_gateway.domain.envelope import EnvelopeError, parse_topic, validate
from fs_gateway.domain.spill import SpillBuffer, SpillFull, SpillRecord, encode
from fs_gateway.metrics import GatewayMetrics
from fs_gateway.settings import Settings
from fscore.sketches import BloomFilter, RotatingBloom
from hypothesis import given, settings
from hypothesis import strategies as st

OEMS = frozenset({"OEM_A", "OEM_B", "OEM_C", "OEM_D"})


# ---------------------------------------------------------------- envelope
def test_parse_topic_and_validate_ok():
    assert parse_topic("telemetry/OEM_A/17") == ("telemetry", "OEM_A", 17)
    env = validate("event/OEM_B/0", b"{}", OEMS)
    assert env.kind == "event" and env.kafka_key == b"OEM_B:0" and env.size == 2


@pytest.mark.parametrize("topic,payload,reason", [
    ("telemetry/OEM_A", b"x", "BAD_TOPIC"),
    ("other/OEM_A/1", b"x", "BAD_TOPIC"),
    ("telemetry/OEM_A/abc", b"x", "BAD_SHARD"),
    ("telemetry/OEM_A/64", b"x", "BAD_SHARD"),
    ("telemetry/OEM_A/-1", b"x", "BAD_SHARD"),
    ("telemetry/OEM_Z/1", b"x", "UNKNOWN_OEM"),
    ("telemetry/OEM_A/1", b"", "EMPTY"),
    ("telemetry/OEM_A/1", b"x" * 11, "TOO_LARGE"),
])
def test_validate_rejects(topic, payload, reason):
    with pytest.raises(EnvelopeError) as e:
        validate(topic, payload, OEMS, max_bytes=10)
    assert e.value.reason == reason


def test_one_megabyte_boundary():
    validate("telemetry/OEM_A/1", b"x" * 1_048_576, OEMS)
    with pytest.raises(EnvelopeError):
        validate("telemetry/OEM_A/1", b"x" * 1_048_577, OEMS)


def test_settings_helpers():
    assert Settings(known_oems="A, B").oems() == frozenset({"A", "B"})


# ---------------------------------------------------------------- dedup
class MemStore:
    def __init__(self, fail=False):
        self.keys: set[str] = set()
        self.fail, self.nx_calls, self.add_calls = fail, 0, 0

    def add_many(self, keys, ttl_s):
        if self.fail:
            raise ConnectionError("down")
        self.add_calls += 1
        self.keys.update(keys)

    def set_nx_many(self, keys, ttl_s):
        if self.fail:
            raise ConnectionError("down")
        self.nx_calls += 1
        out = []
        for k in keys:
            out.append(k not in self.keys)
            self.keys.add(k)
        return out


def make_deduper(store=None, bloom=None, cold=0.0, clock=None):
    return BatchDeduper(store or MemStore(), bloom or RotatingBloom(1000, 0.01), 60, cold_exact_s=cold,
                        metrics=GatewayMetrics(), **({"clock": clock} if clock else {}))


def test_batch_key_is_stable_and_distinguishes_oem_kind_payload():
    assert batch_key("A", "t", b"p") == batch_key("A", "t", b"p")
    assert len({batch_key("A", "t", b"p"), batch_key("B", "t", b"p"), batch_key("A", "e", b"p"), batch_key("A", "t", b"q")}) == 4


def test_new_batches_use_bloom_negative_and_do_not_call_exact_lookup():
    store = MemStore()
    d = make_deduper(store)
    assert d.filter(["a", "b", "c"]) == [True, True, True]
    assert store.nx_calls == 0 and store.add_calls == 1             # ~99% path: no exact lookup


def test_retry_is_dropped_via_maybe_then_exact():
    store = MemStore()
    d = make_deduper(store)
    d.filter(["a", "b"])
    assert d.filter(["a", "c", "b"]) == [False, True, False]
    assert store.nx_calls == 1


def test_duplicate_inside_one_chunk_dropped():
    assert make_deduper().filter(["x", "x", "y", "x"]) == [True, False, True, False]


def test_bloom_false_positive_never_drops_a_valid_batch():
    bloom = BloomFilter(16, 2)                                        # tiny -> saturates -> constant false positives
    class B:
        def __init__(self): self.b = bloom
        def add(self, k): self.b.add(k)
        def __contains__(self, k): return k in self.b
    d = make_deduper(bloom=B())
    keys = [f"k{i}" for i in range(200)]
    assert d.filter(keys) == [True] * 200                             # every key is new; FP resolved by exact store
    assert d.filter(keys) == [False] * 200


def test_fail_open_when_store_down_and_cold_start_uses_exact():
    d = make_deduper(MemStore(fail=True))
    assert d.filter(["a", "a2"]) == [True, True]
    assert d.metrics.store_errors._value.get() == 1
    t = {"now": 0.0}
    store = MemStore()
    store.keys.add("old")
    cold = make_deduper(store, cold=100.0, clock=lambda: t["now"])
    assert cold.filter(["old", "new"]) == [False, True]              # cold: even bloom-negative keys are checked exactly
    t["now"] = 500.0
    assert cold.filter(["new2"]) == [True] and store.nx_calls == 1


@settings(max_examples=60, deadline=None)
@given(st.lists(st.integers(0, 60), max_size=120))
def test_dedup_never_drops_a_unique_key_property(seq):
    d = make_deduper(bloom=RotatingBloom(20, 0.3))
    seen: set[str] = set()
    for i in range(0, len(seq), 7):
        chunk = [f"k{x}" for x in seq[i : i + 7]]
        keep = d.filter(chunk)
        for k, kp in zip(chunk, keep, strict=True):
            assert kp == (k not in seen)                             # exact: kept iff never seen before
            seen.add(k)


# ---------------------------------------------------------------- spill
def rec(i: int, size: int = 10) -> SpillRecord:
    return SpillRecord("ingest.raw.v1", f"OEM_A:{i}".encode(), bytes([i % 256]) * size, {"oem": b"OEM_A", "n": str(i).encode()})


def test_spill_append_peek_ack_in_order(tmp_path):
    sb = SpillBuffer(tmp_path, max_bytes=10_000_000, segment_bytes=300)
    for i in range(20):
        sb.append(rec(i))
    sb.sync()
    assert len(sb) == 20 and len(sb._segs) > 1                        # rolled segments
    first = sb.peek(5)
    assert [r.headers["n"] for _, r in first] == [str(i).encode() for i in range(5)]
    sb.ack(first[-1][0], 5)
    assert len(sb) == 15
    rest = sb.peek(100)
    assert [int(r.headers["n"]) for _, r in rest] == list(range(5, 20))
    sb.ack(rest[-1][0], 15)
    assert len(sb) == 0 and sb.peek(10) == []
    sb.append(rec(99))                                                # reuse after being fully drained
    assert [int(r.headers["n"]) for _, r in sb.peek(5)] == [99]


def test_spill_survives_restart_and_torn_tail(tmp_path):
    sb = SpillBuffer(tmp_path, segment_bytes=10_000)
    for i in range(6):
        sb.append(rec(i))
    sb.sync()
    got = sb.peek(2)
    sb.ack(got[-1][0], 2)
    sb.close()
    seg = sorted(tmp_path.glob("seg-*.log"))[-1]
    with open(seg, "ab") as f:
        f.write(b"\x00\x00\x00\x09garbage-torn-write")               # crash mid-write
    sb2 = SpillBuffer(tmp_path, segment_bytes=10_000)
    assert len(sb2) == 4
    assert [int(r.headers["n"]) for _, r in sb2.peek(10)] == [2, 3, 4, 5]
    sb2.append(rec(6))
    assert [int(r.headers["n"]) for _, r in sb2.peek(10)] == [2, 3, 4, 5, 6]


def test_spill_full_raises_and_recovers_after_ack(tmp_path):
    frame = len(encode(rec(0, 100)))
    sb = SpillBuffer(tmp_path, max_bytes=frame * 3 + 5)
    for i in range(3):
        sb.append(rec(i, 100))
    with pytest.raises(SpillFull):
        sb.append(rec(3, 100))
    items = sb.peek(3)
    sb.ack(items[0][0], 1)
    sb.append(rec(3, 100))
    assert sb.pending_bytes <= sb.max_bytes


def test_spill_corrupt_cursor_file_is_tolerated(tmp_path):
    sb = SpillBuffer(tmp_path)
    sb.append(rec(1))
    sb.close()
    (tmp_path / "cursor.json").write_bytes(b"{broken")
    sb2 = SpillBuffer(tmp_path)
    assert len(sb2) == 1 and os.path.exists(tmp_path)