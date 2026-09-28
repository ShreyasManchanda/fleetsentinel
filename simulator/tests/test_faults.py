from __future__ import annotations

import numpy as np
import pytest
from fs_simulator import groundtruth as G
from fs_simulator.behaviour import SimConfig
from fs_simulator.constants import EVT
from helpers_sim import make_sim

T_MORNING = 1_790_000_000.0 - (1_790_000_000.0 % 86400) - 19800 + 6 * 3600     # 06:00 IST


def _run(fleet, routes, scale, ticks=360, dt=60.0, seed=11, **kw):
    cfg = SimConfig(emit_driving_s=dt, emit_idle_s=dt, emit_parked_s=dt, emit_charging_s=dt, fault_scale=scale,
                    plan_days=0.4, gps_jump_p=2e-3, skew_share=0.05, **kw)
    sim, gt = make_sim(fleet, routes, seed=seed, cfg=cfg, t0=T_MORNING)
    ems = [sim.step(dt) for _ in range(ticks)]
    return sim, gt, ems


@pytest.fixture(scope="module")
def faulty(fleet, routes):
    return _run(fleet, routes, scale=300.0)


def test_every_fault_kind_is_recorded_in_ground_truth(faulty):
    _, gt, _ = faulty
    t = gt.table()
    kinds = set(t["kind"].to_pylist())
    for k in (G.TOW, G.CRASH, G.GOING_DARK, G.STUCK_SPEED, G.ODOMETER_ROLLBACK, G.CLOCK_SKEW, G.GPS_JUMP, G.DEPOT_CLUSTER):
        assert k in kinds, k
    assert len(set(t["incident_id"].to_pylist())) == t.num_rows       # deterministic, unique ids


def test_tow_moves_with_ignition_off(faulty, fleet):
    sim, gt, ems = faulty
    tow = gt.table().filter(np.array(gt.table()["kind"].to_pylist()) == G.TOW).to_pylist()
    assert tow
    vin = tow[0]["vin"]
    gi = int(np.flatnonzero(fleet.vin == vin)[0])
    rows = [(e.ts_ms[i], e.lat[i], e.lon[i], e.ign[i], e.speed[i], e.evt[i]) for e in ems for i in np.flatnonzero(e.gidx == gi)]
    after = [r for r in rows if r[0] >= tow[0]["start_ts_ms"] - 5000]
    assert any(r[5] == EVT["TOW"] for r in after)
    moving_off = [r for r in after if r[4] > 20 and not r[3]]
    assert moving_off, "TOW = movement while ignition is off"
    assert abs(after[-1][1] - after[0][1]) + abs(after[-1][2] - after[0][2]) > 0.01


def test_going_dark_vehicles_emit_nothing_in_window(faulty, fleet):
    _, gt, ems = faulty
    dark = [r for r in gt.table().to_pylist() if r["kind"] == G.GOING_DARK][:10]
    for r in dark:
        gi = int(np.flatnonzero(fleet.vin == r["vin"])[0])
        for e in ems:
            for ts in e.ts_ms[e.gidx == gi]:
                assert not (r["start_ts_ms"] + 5000 < ts < r["end_ts_ms"] - 5000)


def test_crash_freezes_vehicle_and_emits_event(faulty, fleet):
    _, gt, ems = faulty
    crash = [r for r in gt.table().to_pylist() if r["kind"] == G.CRASH]
    assert crash
    gi = int(np.flatnonzero(fleet.vin == crash[0]["vin"])[0])
    evts = np.concatenate([e.evt[e.gidx == gi] for e in ems])
    assert (evts == EVT["CRASH"]).sum() == 1
    speeds = np.concatenate([e.speed[e.gidx == gi] for e in ems])
    assert speeds[-1] == 0.0


def test_depot_cluster_converges_on_one_point(faulty, fleet):
    _, gt, ems = faulty
    rows = [r for r in gt.table().to_pylist() if r["kind"] == G.DEPOT_CLUSTER]
    grp = {}
    for r in rows:
        grp.setdefault(r["group_id"], []).append(r)
    gid, members = max(grp.items(), key=lambda kv: len(kv[1]))
    assert len(members) >= 4
    gis = [int(np.flatnonzero(fleet.vin == m["vin"])[0]) for m in members]
    last = ems[-1]
    pos = np.array([[last.lat[j], last.lon[j]] for gi in gis for j in np.flatnonzero(last.gidx == gi)])
    if len(pos) >= 3:
        assert pos.std(axis=0).max() < 0.02      # clustered within ~2 km


def test_odometer_rollback_and_stuck_and_skew_and_gps(faulty, fleet):
    _, gt, ems = faulty
    rows = gt.table().to_pylist()
    roll = next(r for r in rows if r["kind"] == G.ODOMETER_ROLLBACK)
    gi = int(np.flatnonzero(fleet.vin == roll["vin"])[0])
    o = np.concatenate([e.odo[e.gidx == gi] for e in ems])
    assert (np.diff(o) < -500).any(), "reported odometer must drop (rollback/tamper)"
    stuck = next(r for r in rows if r["kind"] == G.STUCK_SPEED)
    gi = int(np.flatnonzero(fleet.vin == stuck["vin"])[0])
    sp = [(e.ts_ms[i], e.speed[i]) for e in ems for i in np.flatnonzero(e.gidx == gi)
          if stuck["start_ts_ms"] + 1000 < e.ts_ms[i] < stuck["end_ts_ms"] - 1000]
    assert sp and len({round(float(s), 3) for _, s in sp}) == 1
    skew = [r for r in rows if r["kind"] == G.CLOCK_SKEW]
    assert skew and all(60_000 <= abs(r["value"]) <= 600_000 for r in skew)
    assert all(r["value"] >= 0 for r in rows if r["kind"] == G.GPS_JUMP)


def test_breakdown_from_wear_hazard_and_repair(fleet, routes):
    cfg = SimConfig(emit_driving_s=600, emit_idle_s=600, emit_parked_s=600, emit_charging_s=600, plan_days=3)
    sim, gt = make_sim(fleet, routes, seed=8, cfg=cfg, t0=T_MORNING - 6 * 3600)
    sim.wear.w[:] = 1.6                                         # very worn fleet -> high hazard
    for _ in range(3 * 144):
        sim.step(600.0)
    assert gt.count(G.BREAKDOWN) > 20
    assert (sim.wear.w[sim.mode == 0] < 1.6).any()              # repairs reduce wear after the frozen period ends


def test_faults_disabled_produces_no_incidents(fleet, routes):
    cfg = SimConfig(emit_driving_s=60, emit_idle_s=60, emit_parked_s=60, emit_charging_s=60, faults=False)
    sim, gt = make_sim(fleet, routes, cfg=cfg, t0=T_MORNING)
    for _ in range(120):
        sim.step(60.0)
    assert gt.count() == 0
