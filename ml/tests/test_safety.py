"""Tests for the explainable safety score + coaching levers."""
from __future__ import annotations

import numpy as np
from fs_ml.safety import (
    MIN_EXPOSURE_KM,
    DriverExposure,
    coaching_levers,
    compute_safety_score,
    peer_p50,
)
from hypothesis import given
from hypothesis import strategies as st


def _driver(hb=6.0, ha=4.0, over=20.0, night_km=50.0, km=1000.0, idle_h=10.0, assigned_h=100.0):
    return DriverExposure(harsh_brake_count=hb, harsh_accel_count=ha, overspeed_min=over,
                          night_km=night_km, idle_hours_while_assigned=idle_h,
                          assigned_hours=assigned_h, km=km)


def test_score_bounds_and_perfect_driver():
    s = compute_safety_score(DriverExposure(km=1000.0, assigned_hours=50.0))
    assert s.score == 100.0 and s.confidence == "ok"
    bad = compute_safety_score(DriverExposure(harsh_brake_count=500, km=1000.0, assigned_hours=50.0))
    assert bad.score == 0.0


def test_low_confidence_badge_below_minimum_exposure():
    s = compute_safety_score(DriverExposure(harsh_brake_count=1, km=MIN_EXPOSURE_KM - 1, assigned_hours=10.0))
    assert s.confidence == "low"
    ok = compute_safety_score(DriverExposure(harsh_brake_count=1, km=MIN_EXPOSURE_KM, assigned_hours=10.0))
    assert ok.confidence == "ok"


def test_per_100km_normalization_drives_score():
    a = compute_safety_score(_driver(hb=6.0, ha=4.0, over=20.0, night_km=50.0, km=1000.0, idle_h=10.0, assigned_h=100.0))
    b = compute_safety_score(_driver(hb=12.0, ha=8.0, over=40.0, night_km=100.0, km=2000.0, idle_h=20.0, assigned_h=200.0))
    assert a.score == b.score
    assert a.per_100km["harsh_brake"] == 0.6


def test_night_driving_amplifies_penalty():
    day = compute_safety_score(_driver(night_km=0.0))
    night = compute_safety_score(_driver(night_km=500.0))
    assert night.score < day.score


def test_coaching_picks_worst_peer_gap():
    rates = {"harsh_brake": 3.1, "harsh_accel": 0.5, "overspeed_min": 1.0, "idle_share": 0.05}
    med = {"harsh_brake": 1.2, "harsh_accel": 0.4, "overspeed_min": 0.9, "idle_share": 0.05}
    levers = coaching_levers(rates, med)
    assert levers and levers[0].lever == "harsh_brake"
    assert "fleet p50" in levers[0].message
    assert coaching_levers(med, med) == []


def test_peer_p50():
    assert peer_p50({"harsh_brake": np.array([1.0, 2.0, 3.0])}) == {"harsh_brake": 2.0}
    assert peer_p50({"x": np.array([])}) == {"x": 0.0}


@given(
    hb=st.floats(0, 50), ha=st.floats(0, 50), over=st.floats(0, 200),
    km=st.floats(1, 20000), night_frac=st.floats(0, 1),
)
def test_score_always_bounded(hb, ha, over, km, night_frac):
    d = DriverExposure(harsh_brake_count=hb, harsh_accel_count=ha, overspeed_min=over,
                       night_km=night_frac * km, km=km, assigned_hours=100.0,
                       idle_hours_while_assigned=10.0)
    s = compute_safety_score(d)
    assert 0.0 <= s.score <= 100.0
    assert s.confidence in ("ok", "low")


@given(km=st.floats(1, 20000))
def test_exposure_scaling_invariance(km):
    a = compute_safety_score(_driver(hb=6.0, ha=4.0, over=20.0, night_km=0.05 * 1000.0, km=1000.0))
    b = compute_safety_score(_driver(hb=6.0 * km / 1000.0, ha=4.0 * km / 1000.0, over=20.0 * km / 1000.0,
                                     night_km=0.05 * km, km=km))
    assert a.score == b.score
