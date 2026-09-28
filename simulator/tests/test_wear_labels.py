from __future__ import annotations

import numpy as np
import pyarrow as pa
import pytest
from fs_simulator import groundtruth as G
from fs_simulator.behaviour import SimConfig
from fs_simulator.constants import DTC_ALL
from fs_simulator.features import (
    DAY_MS,
    FEATURE_NAMES,
    LeakageError,
    assert_no_leakage,
    build_features,
    maintenance_labels,
)
from fs_simulator.lake import emission_to_table
from fs_simulator.wear import WearModel, dtc_rate_per_hour, hazard_per_day, soh_rate_pct_per_day
from helpers_sim import make_sim
from hypothesis import given, settings
from hypothesis import strategies as st


def test_signals_are_increasing_functions_of_wear():
    w = np.linspace(0, 2.5, 60)
    for f in (dtc_rate_per_hour, soh_rate_pct_per_day, hazard_per_day):
        assert (np.diff(f(w)) >= 0).all() and f(w)[-1] > 3 * f(w)[0]


def test_breakdown_draw_depends_only_on_wear_not_on_observables():
    n = 5000
    a = WearModel(np.random.default_rng(1), n, np.ones(n, bool))
    b = WearModel(np.random.default_rng(1), n, np.ones(n, bool))
    b.soh[:] = 55.0                                     # wildly different observable state, identical hidden w
    ma, da = a.sample_breakdowns(np.random.default_rng(2), np.ones(n, bool))
    mb, db = b.sample_breakdowns(np.random.default_rng(2), np.ones(n, bool))
    assert (ma == mb).all() and (da == db).all()


def test_worn_vehicles_fail_and_alarm_more():
    n = 20_000
    m = WearModel(np.random.default_rng(3), n, np.ones(n, bool))
    m.w[: n // 2], m.w[n // 2:] = 0.3, 1.5
    fail, delay = m.sample_breakdowns(np.random.default_rng(4), np.ones(n, bool))
    assert fail[n // 2:].mean() > 5 * max(fail[: n // 2].mean(), 1e-4)
    assert dtc_rate_per_hour(m.w[n // 2:]).mean() > 10 * dtc_rate_per_hour(m.w[: n // 2]).mean()
    assert 1800 < np.median(delay) < 3 * 86400            # jittered delay, independent of w


def test_wear_advance_random_walk_with_shocks_and_repair():
    n = 4000
    m = WearModel(np.random.default_rng(5), n, np.ones(n, bool))
    w0 = m.w.copy()
    for _ in range(60):
        m.advance_day(np.random.default_rng(int(m.w.sum() * 1000) % 2**31), 1.0)
    assert m.w.mean() > w0.mean() and m.w.std() > 0 and (m.w >= 0).all()
    assert np.nanmax(m.soh) <= 100 and np.nanmin(m.soh) >= 40
    mask = m.w > 1.0
    before = m.w[mask].copy()
    m.repair(mask)
    assert (m.w[mask] < before).all()


def _table(n_veh=6, n_rows=300, seed=0):
    rng = np.random.default_rng(seed)
    vin = np.array([f"V{i}" for i in rng.integers(0, n_veh, n_rows)])
    ts = np.sort(rng.integers(0, 10 * DAY_MS, n_rows))
    dtc = [DTC_ALL[int(rng.integers(0, len(DTC_ALL)))] if rng.random() < 0.2 else None for _ in range(n_rows)]
    return pa.table({"vin": vin.tolist(), "ts_ms": ts, "soh_pct": rng.uniform(80, 100, n_rows).astype(np.float32),
                     "dtc": dtc, "speed_kmh": rng.uniform(0, 60, n_rows).astype(np.float32),
                     "odo_km": np.cumsum(rng.uniform(0, 1, n_rows)).astype(np.float32),
                     "evt": rng.choice(["POSITION", "HARSH_BRAKE", "HARSH_ACCEL"], n_rows).tolist(),
                     "state": rng.integers(0, 4, n_rows).astype(np.int8)})


@settings(max_examples=25, deadline=None)
@given(seed=st.integers(0, 10_000), p_day=st.floats(2.0, 9.0))
def test_features_never_depend_on_data_at_or_after_prediction_time(seed, p_day):
    t = _table(seed=seed)
    p = int(p_day * DAY_MS)
    base = build_features(t, p)
    ts = t["ts_ms"].to_numpy()
    future = ts >= p
    tampered = t.set_column(t.schema.get_field_index("dtc"), "dtc", pa.array(
        [("P0217" if f else d) for f, d in zip(future, t["dtc"].to_pylist(), strict=True)]))
    tampered = tampered.set_column(tampered.schema.get_field_index("soh_pct"), "soh_pct",
                                   pa.array(np.where(future, 1.0, t["soh_pct"].to_numpy()).astype(np.float32)))
    dropped = t.filter(pa.array(~future))
    for other in (build_features(tampered, p), build_features(dropped, p)):
        assert set(other) == set(base)
        for k in base:
            a, b = np.asarray(base[k]), np.asarray(other[k])
            assert a.shape == b.shape and (a == b).all() or np.allclose(a.astype(float), b.astype(float), equal_nan=True)


def test_leakage_guard_raises_and_feature_names_present():
    with pytest.raises(LeakageError):
        assert_no_leakage(1000, 1000)
    assert_no_leakage(999, 1000)
    f = build_features(_table(), 5 * DAY_MS)
    assert set(FEATURE_NAMES) <= set(f)


def test_labels_are_forward_looking_and_censor_free():
    vins = np.array(["A", "B", "C"])
    lab = maintenance_labels(vins, np.array([0, 1, 1]), np.array([2 * DAY_MS, 3 * DAY_MS, 20 * DAY_MS]), [1 * DAY_MS, 2 * DAY_MS], 7.0)
    rows = {(r["vin"], r["prediction_ts_ms"]): r for r in lab.to_pylist()}
    assert rows[("A", DAY_MS)]["will_fail"] and rows[("A", DAY_MS)]["breakdown_ts_ms"] == 2 * DAY_MS
    assert not rows[("A", 2 * DAY_MS)]["will_fail"]                 # breakdown AT p is not in (p, p+h]
    assert rows[("B", DAY_MS)]["will_fail"] and not rows[("C", DAY_MS)]["will_fail"]
    assert rows[("B", 2 * DAY_MS)]["breakdown_ts_ms"] == 3 * DAY_MS  # the day-20 breakdown is beyond the horizon


def _auc(score, y):
    order = np.argsort(score, kind="stable")
    ranks = np.empty(len(score))
    ranks[order] = np.arange(1, len(score) + 1)
    pos = y.sum()
    return (ranks[y].sum() - pos * (pos + 1) / 2) / (pos * (len(y) - pos))


def test_features_carry_signal_without_leaking_label(fleet, routes):
    cfg = SimConfig(emit_driving_s=1800, emit_idle_s=1800, emit_parked_s=1800, emit_charging_s=1800, plan_days=16,
                    fault_scale=0.0)
    t0 = 1_790_000_000.0 - (1_790_000_000.0 % 86400)
    gt = G.GroundTruth(fleet.vin)
    sim, _ = make_sim(fleet, routes, seed=21, cfg=cfg, gt=gt, t0=t0)
    sim.wear.w += 0.6                                      # accelerate the wear -> enough events in a short test
    tables = []
    for i in range(16 * 48):
        em = sim.step(1800.0)
        if i < 8 * 48:
            tables.append(emission_to_table(em, fleet.vin))
    rows = pa.concat_tables(tables)
    p = int((t0 + 8 * 86400) * 1000)
    feats = build_features(rows, p, window_days=8)
    bd = [r for r in gt.table().to_pylist() if r["kind"] == G.BREAKDOWN]
    vin_index = {v: i for i, v in enumerate(fleet.vin.tolist())}
    lab = maintenance_labels(fleet.vin, np.array([vin_index[r["vin"]] for r in bd]), np.array([r["start_ts_ms"] for r in bd]), [p], 8.0)
    y_by_vin = dict(zip(lab["vin"].to_pylist(), lab["will_fail"].to_pylist(), strict=True))
    y = np.array([y_by_vin[v] for v in feats["vin"]])
    assert y.sum() >= 10
    assert _auc(feats["dtc_rows"] + 0.5 * feats["critical_dtc_rows"], y) > 0.6      # signal is learnable
    assert _auc(feats["dtc_rows"] + 0.5 * feats["critical_dtc_rows"], y) < 0.98     # ... but not a copy of the label
