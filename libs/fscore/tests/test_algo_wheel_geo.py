import math
import random

import pytest
from fscore.algorithms import (
    EventTimeWindows,
    TimingWheel,
    UnionFind,
    astar,
    decode,
    dijkstra,
    encode,
    grid_graph,
    haversine_km,
    nearest_reachable,
    neighbours,
    path_cost,
)
from fscore.algorithms.graph import RoadGraph
from hypothesis import given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------- timing wheel
def test_wheel_fires_at_deadline_not_before():
    w = TimingWheel(tick_ms=1000)
    w.schedule("a", 10_500)
    w.schedule("b", 5_000)
    assert w.advance(1_000) == []            # first advance only starts the clock
    assert w.advance(4_999) == []
    assert w.advance(5_000) == [("b", 5_000)]
    assert w.advance(10_499) == []
    assert w.advance(11_000) == [("a", 10_500)]
    assert len(w) == 0 and w.advance(50_000) == []


def test_wheel_reschedule_cancel_and_past_deadline():
    w = TimingWheel(tick_ms=1000)
    w.schedule("a", 60_000)
    w.advance(0)
    w.schedule("a", 90_000)                  # reschedule replaces
    w.schedule("c", 1)                       # deadline in the past fires on the next tick
    assert "a" in w and w.deadline("a") == 90_000
    assert [k for k, _ in w.advance(2_000)] == ["c"]
    assert w.cancel("a") and not w.cancel("a")
    assert w.advance(200_000) == []


def test_wheel_cascades_across_levels_and_overflow_and_big_jump():
    w = TimingWheel(tick_ms=1, slot_bits=2, levels=3)   # horizon 64 ticks
    w.advance(0)
    for d in (1, 5, 17, 40, 63, 64, 100, 1000):
        w.schedule(d, d)
    fired = []
    for t in range(1, 1100):
        fired += [k for k, _ in w.advance(t)]
    assert fired == [1, 5, 17, 40, 63, 64, 100, 1000]
    w2 = TimingWheel(tick_ms=1)
    w2.advance(0)
    for d in (10, 5000, 10_000_000):
        w2.schedule(d, d)
    assert [k for k, _ in w2.advance(9_000_000)] == [10, 5000]   # rebuild path (huge jump)
    assert [k for k, _ in w2.advance(10_000_000)] == [10_000_000]


def test_wheel_validation():
    with pytest.raises(ValueError):
        TimingWheel(tick_ms=0)


@settings(deadline=None, max_examples=80)
@given(
    st.lists(st.integers(0, 200_000), min_size=1, max_size=60),
    st.lists(st.integers(1, 30_000), min_size=1, max_size=40),
)
def test_wheel_property_exactly_once_never_early(deadlines, steps):
    """Fires exactly once, never before its deadline, and within one tick of the watermark reaching it."""
    tick = 1000
    w = TimingWheel(tick_ms=tick, slot_bits=3, levels=3)
    w.advance(0)
    for i, d in enumerate(deadlines):
        w.schedule(i, d)
    wm = 0
    fired: dict[int, int] = {}
    for s in steps:
        wm += s
        for key, dl in w.advance(wm):
            assert key not in fired
            assert dl <= wm                       # never early
            fired[key] = wm
    wm += 400_000
    for key, dl in w.advance(wm):
        assert key not in fired and dl <= wm
        fired[key] = wm
    assert set(fired) == set(range(len(deadlines)))    # every entry fired exactly once


# ---------------------------------------------------------------- windows
def test_windows_late_events_counted_not_applied():
    w = EventTimeWindows(60_000, 10_000, factory=lambda: [0])
    bump = lambda a: a.__setitem__(0, a[0] + 1)  # noqa: E731
    assert w.add("v", 5_000, bump) and w.add("v", 70_000, bump)
    assert [(k, s) for k, s, _ in w.drain_closed()] == [("v", 0)]   # watermark 60_000 closes [0,60000)
    assert w.add("v", 130_000, bump)              # watermark 120_000: windows 0 and 60000 close
    closed = w.drain_closed()
    assert [(k, s, a[0]) for k, s, a in closed] == [("v", 60000, 1)]
    assert w.add("v", 1_000, bump) is False and w.late_events == 1
    assert w.add("v", 125_000, bump) is True      # older than max but window still open
    snap = w.snapshot()
    w2 = EventTimeWindows(60_000, 10_000, factory=lambda: [0])
    w2.restore(snap)
    assert w2.open_count() == w.open_count() and w2.watermark == w.watermark and w2.late_events == 1
    with pytest.raises(ValueError):
        EventTimeWindows(0)


@given(st.lists(st.integers(0, 600_000), max_size=200))
def test_windows_every_event_applied_or_counted_late(tss):
    w = EventTimeWindows(60_000, 10_000, factory=lambda: [0])
    for ts in tss:
        w.add("k", ts, lambda a: a.__setitem__(0, a[0] + 1))
    assert w.applied_events + w.late_events == len(tss)
    total = sum(a[0] for _, _, a in w.drain_closed()) + sum(v[0] for v in w._open.values())
    assert total == w.applied_events


# ---------------------------------------------------------------- geohash
def test_geohash_known_values():
    assert encode(57.64911, 10.40744, 11) == "u4pruydqqvj"
    assert encode(12.9716, 77.5946, 5).startswith("tdr1")
    lat, lon = decode("u4pruydqqvj")
    assert lat == pytest.approx(57.64911, abs=1e-5) and lon == pytest.approx(10.40744, abs=1e-5)
    with pytest.raises(ValueError):
        encode(91, 0)
    with pytest.raises(ValueError):
        encode(0, 0, 13)


@given(st.floats(-89.99, 89.99), st.floats(-179.99, 179.99), st.integers(1, 9))
def test_geohash_roundtrip_contains_point_and_neighbours_adjacent(lat, lon, prec):
    gh = encode(lat, lon, prec)
    from fscore.algorithms.geohash import decode_bbox
    la0, la1, lo0, lo1 = decode_bbox(gh)
    assert la0 <= lat <= la1 and lo0 <= lon <= lo1
    ns = neighbours(gh)
    assert gh not in ns and len(set(ns)) == len(ns) and 3 <= len(ns) <= 8
    for n in ns:
        assert len(n) == prec
        assert encode(*decode(n), prec) == n


def test_geohash_neighbours_wrap_antimeridian_and_poles():
    gh = encode(0.0, 179.9999, 5)
    assert len(neighbours(gh)) == 8
    assert any(decode(n)[1] < 0 for n in neighbours(gh))
    assert len(neighbours(encode(89.9999, 10, 4))) == 5


# ---------------------------------------------------------------- union-find
def test_union_find_components():
    uf = UnionFind("abcdef")
    assert uf.union("a", "b") and uf.union("b", "c") and not uf.union("a", "c")
    uf.union("x", "y")
    assert uf.connected("a", "c") and not uf.connected("a", "x") and not uf.connected("a", "zz")
    assert uf.size_of("a") == 3 and len(uf) == 8
    assert sorted(map(len, uf.components())) == [1, 1, 1, 2, 3]
    assert "a" in uf


@given(st.lists(st.tuples(st.integers(0, 30), st.integers(0, 30)), max_size=80))
def test_union_find_matches_naive_reachability(edges):
    uf = UnionFind()
    adj: dict[int, set[int]] = {}
    for a, b in edges:
        uf.union(a, b)
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    for a in adj:
        seen, stack = {a}, [a]
        while stack:
            for nb in adj[stack.pop()]:
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        assert all(uf.connected(a, b) for b in seen) and uf.size_of(a) == len(seen)


# ---------------------------------------------------------------- haversine / A*
def test_haversine_known_distance():
    assert haversine_km(12.9716, 77.5946, 13.0827, 80.2707) == pytest.approx(290, abs=5)
    assert haversine_km(1, 1, 1, 1) == 0


def _random_graph(seed: int, n: int = 40):
    rng = random.Random(seed)
    g = RoadGraph()
    for i in range(n):
        g.add_node(i, 12.8 + rng.random() * 0.3, 77.4 + rng.random() * 0.3)
    for i in range(1, n):
        j = rng.randrange(i)
        d = haversine_km(*g.pos[i], *g.pos[j]) * (1.0 + rng.random())
        g.add_edge(i, j, d, d * (0.15 + rng.random() * 0.1))
    for _ in range(n):
        a, b = rng.randrange(n), rng.randrange(n)
        if a != b:
            d = haversine_km(*g.pos[a], *g.pos[b]) * (1.0 + rng.random())
            g.add_edge(a, b, d, d * (0.15 + rng.random() * 0.1))
    return g


@settings(deadline=None, max_examples=60)
@given(st.integers(0, 10_000), st.integers(0, 39), st.integers(0, 39), st.floats(0.5, 30))
def test_astar_cost_equals_dijkstra_cost(seed, s, t, budget):
    g = _random_graph(seed)
    a, d = astar(g, s, t, budget), dijkstra(g, s, t, budget)
    assert a.found == d.found
    if a.found:
        assert a.cost == pytest.approx(d.cost, rel=1e-9)
        assert path_cost(g, a.path) == pytest.approx(a.cost, rel=1e-9)
        assert a.path[0] == s and a.path[-1] == t and a.cost <= budget + 1e-9


def test_astar_expands_fewer_nodes_than_dijkstra_on_grid():
    g, snap = grid_graph(12.85, 77.50, 13.10, 77.75, 0.8)
    s, t = snap(12.86, 77.51), snap(13.09, 77.74)
    a, d = astar(g, s, t), dijkstra(g, s, t)
    assert a.cost == pytest.approx(d.cost) and a.expansions < d.expansions
    assert astar(g, s, "nope").found is False and astar(g, "nope", t).found is False


def test_nearest_reachable_charger_respects_soc_budget():
    g, snap = grid_graph(12.85, 77.50, 13.10, 77.75, 1.0, cost_per_km=0.2)
    chargers = {"near": snap(12.95, 77.60), "far": snap(13.08, 77.74)}
    start = snap(12.93, 77.58)
    r = nearest_reachable(g, start, chargers, budget=50.0)
    assert r.found and r.goal == "near"
    base = nearest_reachable(g, start, chargers, budget=50.0, use_heuristic=False)
    assert base.goal == "near" and base.cost == pytest.approx(r.cost) and r.expansions <= base.expansions
    none = nearest_reachable(g, start, chargers, budget=0.01)
    assert not none.found
    assert nearest_reachable(g, "x", chargers, 10).found is False
    assert nearest_reachable(g, start, {}, 10).found is False
    assert math.isinf(none.cost)