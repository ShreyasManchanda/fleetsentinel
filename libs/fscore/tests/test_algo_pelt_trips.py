import time

import pytest
from fscore.algorithms import (
    IdleDetector,
    Sample,
    TariffRow,
    TripDetector,
    TripParams,
    idling_cost,
    price_at,
    segment_naive,
    segment_pelt,
    segment_trips,
    segments,
)
from hypothesis import given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------- PELT
def test_pelt_finds_obvious_changepoints():
    y = [0.0] * 30 + [50.0] * 30 + [0.0] * 30
    cps, cost = segment_pelt(y, 10.0)
    assert cps == [30, 60]
    assert cost == pytest.approx(3 * 10.0)
    assert segments(cps, len(y)) == [(0, 30), (30, 60), (60, 90)]
    assert segment_pelt([], 1.0) == ([], 0.0) and segment_naive([], 1.0) == ([], 0.0)


@settings(deadline=None, max_examples=80)
@given(st.lists(st.floats(-100, 100, allow_nan=False), min_size=1, max_size=60), st.floats(0.1, 500))
def test_pelt_cost_not_worse_than_naive_and_equal(y, pen):
    """PELT is exact for this cost: its optimum equals the O(n^2) baseline (and is never above it)."""
    _, c_pelt = segment_pelt(y, pen)
    cps_n, c_naive = segment_naive(y, pen)
    assert c_pelt <= c_naive + 1e-6 * max(1.0, abs(c_naive))
    assert c_pelt == pytest.approx(c_naive, rel=1e-6, abs=1e-6)


def test_pelt_is_faster_than_naive_on_long_series():
    import random
    rng = random.Random(3)
    y = []
    for level in (0, 40, 0, 60, 5, 0):
        y += [level + rng.gauss(0, 1) for _ in range(200)]
    t0 = time.perf_counter()
    cp, c1 = segment_pelt(y, 30.0)
    t_p = time.perf_counter() - t0
    t0 = time.perf_counter()
    cn, c2 = segment_naive(y, 30.0)
    t_n = time.perf_counter() - t0
    assert cp == cn and c1 == pytest.approx(c2) and t_p < t_n


def test_segment_trips_labels_moving_and_stopped():
    speeds = [0.0] * 40 + [45.0] * 40 + [0.0] * 40 + [60.0] * 30
    seg = segment_trips(speeds)
    assert [m for _, _, m in seg] == [False, True, False, True]
    assert seg[0][0] == 0 and seg[-1][1] == len(speeds)
    assert segment_trips([]) == []
    assert segment_trips([0.0, 0.0, 0.0], penalty=5.0) == [(0, 3, False)]


# ---------------------------------------------------------------- idling cost
ROWS = [
    TariffRow("DIESEL", 0, 8, 1.0), TariffRow("DIESEL", 8, 20, 1.2), TariffRow("DIESEL", 20, 24, 1.0),
    TariffRow("ELECTRICITY", 0, 24, 0.2, "kWh"),
]


def test_price_lookup_and_fallback():
    assert price_at(ROWS, "DIESEL", 9) == 1.2 and price_at(ROWS, "DIESEL", 7) == 1.0
    assert price_at(ROWS, "PETROL", 9) is None


def test_idling_cost_splits_at_hour_boundaries_and_timezone():
    hour = 3_600_000
    base = 1_700_000_000_000 - (1_700_000_000_000 % (24 * hour))         # midnight UTC
    # 07:30-08:30 local (UTC): half an hour at 1.0 and half an hour at 1.2, burn 2 L/h -> 1.0*1.0 + 1.0*1.2
    c = idling_cost(base + 7 * hour + hour // 2, base + 8 * hour + hour // 2, ROWS, "DIESEL", 2.0)
    assert c.cost_usd == pytest.approx(2.2) and c.quantity == pytest.approx(2.0) and c.seconds == 3600
    # same instants but tz=+330: 13:00-14:00 local -> all at 1.2
    c2 = idling_cost(base + 7 * hour + hour // 2, base + 8 * hour + hour // 2, ROWS, "DIESEL", 2.0, 330)
    assert c2.cost_usd == pytest.approx(2.4)
    assert idling_cost(10, 5, ROWS, "DIESEL", 2).cost_usd == 0
    assert idling_cost(0, hour, ROWS, "PETROL", 3.0).cost_usd == 0        # no tariff: zero price
    ev = idling_cost(0, hour, ROWS, "ELECTRICITY", 1.5)
    assert ev.cost_usd == pytest.approx(0.3)


@given(st.integers(0, 10**12), st.integers(1, 10 * 3_600_000), st.floats(0.1, 20), st.integers(-720, 840))
def test_idling_cost_matches_constant_tariff_and_is_additive(start, dur, burn, tz):
    flat = [TariffRow("DIESEL", 0, 24, 1.5)]
    c = idling_cost(start, start + dur, flat, "DIESEL", burn, tz)
    assert c.cost_usd == pytest.approx(burn * dur / 3_600_000 * 1.5, rel=1e-9)
    mid = start + dur // 2
    a = idling_cost(start, mid, ROWS, "DIESEL", burn, tz)
    b = idling_cost(mid, start + dur, ROWS, "DIESEL", burn, tz)
    whole = idling_cost(start, start + dur, ROWS, "DIESEL", burn, tz)
    assert a.cost_usd + b.cost_usd == pytest.approx(whole.cost_usd, rel=1e-9, abs=1e-12)


# ---------------------------------------------------------------- trips + idle
def S(t, sp, ign=True, lat=12.9, lon=77.6):
    return Sample(t * 1000, sp, ign, lat, lon)


def drive(det, times, speed, lat0=12.9):
    out = []
    for i, t in enumerate(times):
        r = det.update(S(t, speed, True, lat0 + i * 0.001))
        if r:
            out.append(r)
    return out


def test_trip_starts_after_confirmation_and_survives_a_red_light():
    d = TripDetector("V")
    d.update(S(0, 0.0, True))
    d.update(S(10, 30.0))
    assert not d.in_trip
    d.update(S(20, 32.0))
    assert d.in_trip
    assert d.update(S(30, 0.0)) is None                # red light: 30 s stop < 120 s hold
    assert d.update(S(60, 0.0)) is None
    d.update(S(70, 40.0))
    assert d.in_trip
    d.update(S(80, 0.0))
    trip = d.update(S(210, 0.0))                       # 130 s stopped -> trip ends
    assert trip is not None and not d.in_trip
    assert trip.start_ts_ms == 10_000 and trip.end_ts_ms == 70_000
    assert trip.max_speed_kmh == 40.0 and trip.distance_km >= 0


def test_trip_ends_immediately_on_ignition_off_and_ignores_out_of_order_and_gps_jump():
    d = TripDetector("V", TripParams(start_confirm=1))
    d.update(S(0, 20.0, True, 12.9, 77.6))
    d.update(S(10, 20.0, True, 12.9005, 77.6))
    d.update(S(5, 99.0))                                # out of order: ignored
    d.update(S(20, 20.0, True, 30.0, 90.0))            # teleport: distance leg excluded
    trip = d.update(S(30, 0.0, False))
    assert d.out_of_order == 1 and trip is not None
    assert trip.distance_km < 1.0


def test_trip_snapshot_restore_mid_trip_is_lossless():
    d = TripDetector("V")
    for t, sp in [(0, 20), (10, 25), (20, 30), (30, 0)]:
        d.update(S(t, float(sp)))
    d2 = TripDetector.restore("V", d.snapshot())
    assert d2.update(S(200, 0.0)) == d.update(S(200, 0.0))


def test_short_blip_never_starts_a_trip():
    d = TripDetector("V")
    for t, sp in [(0, 20), (10, 0), (20, 20), (30, 0)]:
        assert d.update(S(t, float(sp))) is None
    assert not d.in_trip


def test_idle_detector_segments_and_gaps():
    i = IdleDetector("V", min_idle_ms=120_000)
    assert i.update(S(0, 0.0)) is None and i.idle_ms == 0
    for t in (30, 60, 90, 150):
        assert i.update(S(t, 0.0)) is None
    assert i.idle_ms == 150_000
    seg = i.update(S(160, 30.0))
    assert seg is not None and seg.seconds == 150 and seg.start_ts_ms == 0
    j = IdleDetector("V", min_idle_ms=60_000, max_gap_ms=300_000)
    j.update(S(0, 0.0))
    j.update(S(100, 0.0))
    seg2 = j.update(S(1000, 0.0))                       # huge gap closes the previous segment
    assert seg2 is not None and seg2.seconds == 100
    k = IdleDetector("V")
    k.update(S(0, 0.0))
    k.update(S(30, 0.0))
    assert k.update(S(40, 0.0, ign=False)) is None      # too short to report
    m = IdleDetector("V", min_idle_ms=1000)
    m.update(S(0, 0.0))
    m.update(S(50, 0.0))
    m2 = IdleDetector("V", min_idle_ms=1000)
    m2.restore_state(m.snapshot())
    assert m2.idle_ms == 50_000