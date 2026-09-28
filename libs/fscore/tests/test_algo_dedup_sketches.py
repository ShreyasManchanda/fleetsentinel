import math
import random

import pytest
from fscore.algorithms.antireplay import WINDOW, AntiReplayStore, AntiReplayWindow, RecentSet, Verdict
from fscore.sketches import BloomFilter, CountMinSketch, HyperLogLog, RotatingBloom, TopK, optimal_params
from hypothesis import given, settings
from hypothesis import strategies as st


def test_window_basic_flow():
    w = AntiReplayWindow()
    assert w.check_and_update(5) is Verdict.NEW
    assert w.check_and_update(5) is Verdict.DUPLICATE
    assert w.check_and_update(3) is Verdict.NEW        # out of order, inside window
    assert w.check_and_update(3) is Verdict.DUPLICATE
    assert w.check_and_update(5 + WINDOW) is Verdict.NEW
    assert w.check_and_update(5) is Verdict.TOO_OLD    # fell out of the window
    assert w.seen(5 + WINDOW) is True and w.seen(5 + WINDOW + 1) is False and w.seen(5) is None


def test_window_big_jump_resets_bitmap():
    w = AntiReplayWindow()
    w.check_and_update(1)
    w.check_and_update(1000)
    assert w.check_and_update(1) is Verdict.TOO_OLD
    assert w.check_and_update(999) is Verdict.NEW
    assert w.check_and_update(999) is Verdict.DUPLICATE


@given(st.lists(st.integers(min_value=0, max_value=5000), max_size=300))
def test_dedup_matches_exact_set_when_disorder_is_bounded(seqs):
    """Property: inside the window the verdict is EXACT; a unique seq is never reported DUPLICATE
    (TOO_OLD is reported explicitly when it fell out of the window)."""
    w = AntiReplayWindow()
    seen: set[int] = set()
    mx = -1
    for s in seqs:
        v = w.check_and_update(s)
        if s in seen:
            assert v in (Verdict.DUPLICATE, Verdict.TOO_OLD)
        elif mx >= 0 and mx - s >= WINDOW:
            assert v is Verdict.TOO_OLD
        else:
            assert v is Verdict.NEW, (s, mx)
        if v is Verdict.NEW:
            seen.add(s)
        mx = max(mx, s)


@given(st.lists(st.tuples(st.sampled_from(["A", "B", "C"]), st.integers(0, 200)), max_size=200))
def test_store_never_drops_unique_vin_seq(pairs):
    store = AntiReplayStore()
    accepted = set()
    for vin, seq in pairs:
        v = store.check_and_update(vin, seq)
        if v is Verdict.NEW:
            assert (vin, seq) not in accepted
            accepted.add((vin, seq))
        elif v is Verdict.DUPLICATE:
            assert (vin, seq) in accepted
    for vin in "ABC":
        seqs = [s for (v, s) in pairs if v == vin]
        if seqs:
            mx = max(seqs)
            for s in {s for s in seqs if mx - s < WINDOW}:
                assert (vin, s) in accepted


def test_store_snapshot_restore_roundtrip():
    s = AntiReplayStore()
    for i in [1, 2, 4, 3]:
        s.check_and_update("V", i)
    s2 = AntiReplayStore.restore({k: list(v) for k, v in s.snapshot().items()})
    assert len(s2) == 1
    assert s2.check_and_update("V", 3) is Verdict.DUPLICATE and s2.check_and_update("V", 5) is Verdict.NEW


def test_recent_set_bounded_and_exact():
    r = RecentSet(3)
    assert r.add_if_new("a") and not r.add_if_new("a")
    for k in "bcd":
        r.add_if_new(k)
    assert len(r) == 3 and r.add_if_new("a")           # a was evicted (documented bound)
    r2 = RecentSet.restore([["x", 1], "y"], 5)
    assert not r2.add_if_new(("x", 1)) and not r2.add_if_new("y")
    assert len(r2.snapshot()) == 2
    with pytest.raises(ValueError):
        RecentSet(0)


def test_bloom_no_false_negatives_and_fp_rate_close_to_theory():
    bf = BloomFilter.for_capacity(5000, 0.01)
    for i in range(5000):
        bf.add(f"k{i}")
    assert all(f"k{i}" in bf for i in range(5000))
    fp = sum(f"other{i}" in bf for i in range(20000)) / 20000
    assert fp < 0.02 and bf.expected_fp_rate() == pytest.approx(0.01, rel=0.5)
    assert optimal_params(1_000_000, 0.01) == (9585059, 7)
    bf.clear()
    assert "k1" not in bf
    with pytest.raises(ValueError):
        optimal_params(0, 0.5)
    with pytest.raises(ValueError):
        BloomFilter(4, 1)


@given(st.sets(st.binary(min_size=1, max_size=16), max_size=100))
def test_bloom_never_false_negative_property(items):
    bf = BloomFilter.for_capacity(200, 0.05)
    for i in items:
        bf.add(i)
    assert all(i in bf for i in items)


def test_rotating_bloom_keeps_recent_and_is_bounded():
    rb = RotatingBloom(100, 0.01)
    for i in range(150):
        rb.add(f"x{i}")
    assert "x149" in rb and "x120" in rb
    for i in range(150, 400):
        rb.add(f"x{i}")
    assert "x399" in rb


@settings(deadline=None, max_examples=60)
@given(st.lists(st.sampled_from([f"P{i:04X}" for i in range(60)]), min_size=1, max_size=400))
def test_cms_never_undercounts(items):
    cms = CountMinSketch(width=64, depth=4)
    truth: dict[str, int] = {}
    for it in items:
        cms.add(it)
        truth[it] = truth.get(it, 0) + 1
    assert all(cms.estimate(k) >= v for k, v in truth.items())
    assert cms.total == len(items)


def test_cms_error_bound_and_merge_and_bytes():
    rng = random.Random(1)
    cms = CountMinSketch(2048, 5)
    truth: dict[str, int] = {}
    for _ in range(20000):
        k = f"c{int(rng.paretovariate(1.2)) % 500}"
        cms.add(k)
        truth[k] = truth.get(k, 0) + 1
    eps = math.e / 2048 * cms.total
    bad = sum(cms.estimate(k) - v > eps for k, v in truth.items())
    assert bad <= 0.01 * len(truth) + 1
    other = CountMinSketch.from_bytes(cms.to_bytes(), 2048, 5, cms.total)
    assert other.estimate("c1") == cms.estimate("c1")
    cms.merge(other)
    assert cms.estimate("c1") >= 2 * truth["c1"]
    with pytest.raises(ValueError):
        cms.merge(CountMinSketch(10, 2))
    with pytest.raises(ValueError):
        CountMinSketch(0, 1)
    with pytest.raises(ValueError):
        cms.add("x", -1)


def test_topk_finds_heavy_hitters():
    tk = TopK(3, CountMinSketch(1024, 4))
    stream = ["A"] * 50 + ["B"] * 30 + ["C"] * 20 + [f"n{i}" for i in range(200)] + ["A"] * 10
    random.Random(2).shuffle(stream)
    for x in stream:
        tk.update(x)
    assert [n for n, _ in tk.top()][:3] == ["A", "B", "C"]
    tk2 = TopK(3, tk.cms)
    tk2.restore_items(tk.snapshot())
    assert tk2.top() == tk.top()
    with pytest.raises(ValueError):
        TopK(0)


def test_topk_heap_compaction():
    tk = TopK(2, CountMinSketch(512, 3))
    for _ in range(500):
        tk.update("A")
        tk.update("B")
    assert len(tk._heap) <= 8 * tk.k + 64 + 2
    assert {n for n, _ in tk.top()} == {"A", "B"}


def test_hll_accuracy_and_semantics():
    h = HyperLogLog(14)
    assert h.count() == 0
    for i in range(50000):
        h.add(f"v{i}")
    assert abs(h.count() - 50000) / 50000 < 3 * h.standard_error()
    assert h.add("v1") is False
    small = HyperLogLog(14)
    for i in range(100):
        small.add(f"s{i}")
    assert abs(small.count() - 100) <= 3
    a, b = HyperLogLog(10), HyperLogLog(10)
    for i in range(1000):
        a.add(f"x{i}")
    for i in range(500, 1500):
        b.add(f"x{i}")
    a.merge(b)
    assert abs(a.count() - 1500) / 1500 < 0.1
    assert HyperLogLog.from_bytes(a.to_bytes(), 10).count() == a.count()
    with pytest.raises(ValueError):
        a.merge(HyperLogLog(11))
    with pytest.raises(ValueError):
        HyperLogLog(2)
    with pytest.raises(ValueError):
        HyperLogLog.from_bytes(b"x", 10)