from __future__ import annotations

import numpy as np
from fs_simulator.behaviour import Emission, SimConfig
from fs_simulator.constants import CHARGING, DRIVING, EVT, IDLING, PARKED
from helpers_sim import T0, make_sim, run
from hypothesis import given, settings
from hypothesis import strategies as st


def _cat(ems):
    return {k: np.concatenate([getattr(e, k) for e in ems]) for k in Emission.__dataclass_fields__}


def test_deterministic_given_seed(fleet, routes):
    a, _ = make_sim(fleet, routes, seed=9)
    b, _ = make_sim(fleet, routes, seed=9)
    c, _ = make_sim(fleet, routes, seed=10)
    ea, eb, ec = _cat(run(a, 30)), _cat(run(b, 30)), _cat(run(c, 30))
    assert (ea["lat"] == eb["lat"]).all() and (ea["seq"] == eb["seq"]).all() and (ea["ts_ms"] == eb["ts_ms"]).all()
    assert len(ea["lat"]) != len(ec["lat"]) or not (ea["speed"] == ec["speed"]).all()


def test_all_four_states_and_event_types_occur(fleet, routes):
    sim, _ = make_sim(fleet, routes, seed=2, t0=T0 - 12 * 3600 + 0.0)
    ems = _cat(run(sim, 24 * 60, dt=60.0))
    assert {PARKED, DRIVING, IDLING, CHARGING} <= set(np.unique(ems["state"]).tolist())
    seen = set(np.unique(ems["evt"]).tolist())
    for name in ("HARSH_BRAKE", "HARSH_ACCEL", "IGNITION_ON", "IGNITION_OFF", "CHARGING_START"):
        assert EVT[name] in seen, name


def test_value_ranges_and_invariants(fleet, routes):
    sim, _ = make_sim(fleet, routes, seed=3)
    d = _cat(run(sim, 300, dt=120.0))
    assert np.isfinite(d["lat"]).all() and np.isfinite(d["lon"]).all()
    assert (d["speed"] >= 0).all() and d["speed"].max() < 120
    for k in ("soc", "fuel"):
        v = d[k][~np.isnan(d[k])]
        assert v.size and (v >= 0).all() and (v <= 100).all()
    assert (d["odo"] >= 0).all()
    moving = d["state"] == DRIVING
    assert (d["speed"][moving] > 0).mean() > 0.9 and (d["ign"][moving]).all()
    # powertrain applicability: fuel only for ICE/hybrid, soc only for EV/hybrid
    pt = fleet.powertrain[d["gidx"]]
    assert np.isnan(d["fuel"][pt == 1]).all() and np.isnan(d["soc"][pt == 0]).all()


def test_seq_strictly_increasing_per_vehicle_and_ts_monotone(fleet, routes):
    sim, _ = make_sim(fleet, routes, seed=4, cfg=SimConfig(emit_parked_s=60, emit_driving_s=60, emit_idle_s=60,
                                                          emit_charging_s=60, skew_share=0.0, faults=False))
    d = _cat(run(sim, 100))
    order = np.lexsort((d["ts_ms"], d["gidx"]))
    g, s = d["gidx"][order], d["seq"][order]
    same = g[1:] == g[:-1]
    assert (np.diff(s)[same] > 0).all()


def test_trips_are_recorded_and_consistent(fleet, routes):
    sim, _ = make_sim(fleet, routes, seed=5, t0=T0 - 10 * 3600.0)
    run(sim, 24 * 6, dt=600.0)
    assert sim.trips
    tr = {k: np.concatenate([t[k] for t in sim.trips]) for k in sim.trips[0]}
    assert (tr["end_s"] >= tr["start_s"]).all() and (tr["dist_km"] >= 0).all() and (tr["dist_km"] < 80).all()
    assert (tr["night_share"] >= 0).all() and (tr["night_share"] <= 1.0 + 1e-9).all()
    assert (tr["energy"] >= 0).all()


def test_bursts_triple_emission_period(fleet, routes):
    cfg = SimConfig(emit_driving_s=30, emit_idle_s=30, emit_parked_s=30, emit_charging_s=30, burst_local_hour=8.0)
    sim, _ = make_sim(fleet, routes, cfg=cfg)
    assert sim._rate_mult(1_790_000_000.0 - 1_790_000_000.0 % 86400 - 19800 + 8.1 * 3600) == 3.0
    assert sim._rate_mult(1_790_000_000.0 - 1_790_000_000.0 % 86400 - 19800 + 12 * 3600) == 1.0


@settings(max_examples=12, deadline=None)
@given(dt=st.sampled_from([1.0, 10.0, 60.0, 300.0, 900.0]), seed=st.integers(0, 10_000))
def test_engine_stable_for_any_step(fleet, routes, dt, seed):
    sim, _ = make_sim(fleet, routes, seed=seed, t0=T0 - 6 * 3600.0, subset=np.arange(300))
    ems = [sim.step(dt) for _ in range(20)]
    for e in ems:
        assert np.isfinite(e.lat).all() and np.isfinite(e.odo).all() and (e.speed >= 0).all()
        assert ((e.state >= 0) & (e.state <= 3)).all()
    assert np.isfinite(sim.wear.w).all()


def test_no_collect_returns_none(fleet, routes):
    sim, _ = make_sim(fleet, routes)
    assert sim.step(60.0, collect=False) is None and sim.step(60.0) is not None
