"""ML gap tests: split edges, calibration behaviour, metric branches,
safety-score clamps/determinism, signature determinism, kNN edges."""
from __future__ import annotations

import numpy as np
import pyarrow as pa
import pytest
from fs_ml import evaluate as ev
from fs_ml.features import temporal_split, vin_hash_split, vin_split
from fs_ml.safety import DriverExposure, coaching_levers, compute_safety_score
from fs_ml.signatures import DIM, build_signatures, knn_search
from fs_ml.train import apply_calibrator, fit_calibrator

DAY = 1_757_000_000_000


def _day_table(vins, ts_offsets_h, evts):
    n = len(vins)
    return pa.table({
        "vin": list(vins),
        "ts_ms": pa.array([DAY + int(h * 3_600_000) for h in ts_offsets_h], type=pa.int64()),
        "dtc": [None] * n,
        "evt": list(evts),
        "state": pa.array([1] * n, type=pa.int8()),
        "speed_kmh": pa.array([50.0] * n, type=pa.float32()),
        "odo_km": pa.array([float(i) for i in range(n)], type=pa.float32()),
    })


# ---------------------------------------------------------------- splits
def test_temporal_split_single_time_falls_back_to_halves():
    pred = np.array([5, 5, 5, 5])
    tr, te = temporal_split(pred)
    assert tr.sum() == 2 and te.sum() == 2 and not (tr & te).any() and (tr | te).all()


def test_temporal_split_empty():
    tr, te = temporal_split(np.zeros(0, dtype=np.int64))
    assert len(tr) == 0 and len(te) == 0


def test_vin_split_deterministic_and_param_sensitive():
    vins = np.array([f"V{i:03d}" for i in range(50)], dtype=object)
    tr1, te1 = vin_split(vins)
    tr2, te2 = vin_split(vins)
    assert (tr1 == tr2).all() and (te1 == te2).all()
    _, te_other = vin_split(vins, test_rem=0)
    assert not (te1 == te_other).all()
    h1, h2 = vin_hash_split(vins), vin_hash_split(vins)
    assert (h1[0] == h2[0]).all()  # stable as the fleet grows
    big = np.array([f"VIN{i:05d}" for i in range(2000)], dtype=object)
    share = vin_hash_split(big)[1].mean()
    assert 0.10 < share < 0.30  # ~20% held out


# ---------------------------------------------------------------- calibration
def _cal():
    rng = np.random.default_rng(0)
    p = rng.uniform(0, 1, 300)
    y = (rng.uniform(0, 1, 300) < p).astype(float)
    return fit_calibrator(p, y)


def test_calibrator_grid_and_monotone_mapping():
    cal = _cal()
    assert len(cal["x"]) == 101 and len(cal["y"]) == 101
    xs = np.linspace(0, 1, 50)
    mapped = apply_calibrator(cal, xs)
    assert (np.diff(mapped) >= -1e-12).all()
    assert ((mapped >= 0.0) & (mapped <= 1.0)).all()


def test_calibrator_clamps_out_of_range():
    cal = _cal()
    out = apply_calibrator(cal, np.array([-1.0, 2.0]))
    assert ((out >= 0.0) & (out <= 1.0)).all()


# ---------------------------------------------------------------- metrics
def test_brier_empty_and_perfect():
    assert np.isnan(ev.brier(np.zeros(0), np.zeros(0)))
    assert ev.brier(np.array([0, 1]), np.array([0.0, 1.0])) == 0.0


def test_precision_at_k_beyond_n():
    y = np.array([True, False, False])
    assert ev.precision_at_k(y, np.array([0.1, 0.9, 0.2]), 10) == pytest.approx(1 / 3)


def test_expected_savings_zero_k_and_all_negative_ev():
    y = np.array([1, 1, 0])
    p = np.array([0.9, 0.8, 0.1])
    s = ev.expected_savings(y, p, k=0, c_breakdown=2500.0, c_inspection=150.0)
    assert s["n_inspected"] == 0 and s["realized_usd"] == 0.0
    s = ev.expected_savings(y, np.zeros(3), k=3, c_breakdown=2500.0, c_inspection=150.0)
    assert s["n_inspected"] == 0 and s["failures_caught"] == 0.0


def test_calibration_counts_cover_all_rows():
    rng = np.random.default_rng(1)
    y = (rng.uniform(0, 1, 100) < 0.3).astype(int)
    p = rng.uniform(0, 1, 100)
    cal = ev.calibration_data(y, p)
    assert len(cal["edges"]) == 11 and len(cal["count"]) == 10
    assert sum(cal["count"]) == 100


def test_pr_auc_perfect_and_evaluate_shape():
    assert ev.pr_auc(np.array([0, 0, 1, 1]), np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    m = ev.evaluate_predictions(np.array([0, 1, 1, 0]), np.array([0.1, 0.9, 0.8, 0.2]), k=2)
    assert set(m) == {"pr_auc", "precision_at_k", "brier", "calibration", "savings", "n", "positives"}
    assert m["n"] == 4 and m["positives"] == 2


# ---------------------------------------------------------------- safety
def test_safety_deterministic_and_road_class_default():
    d = DriverExposure(harsh_brake_count=6.0, km=1000.0, assigned_hours=50.0)
    assert compute_safety_score(d).score == compute_safety_score(d).score
    urban = compute_safety_score(DriverExposure(harsh_brake_count=6.0, km=1000.0, assigned_hours=50.0))
    unknown = compute_safety_score(DriverExposure(harsh_brake_count=6.0, km=1000.0, assigned_hours=50.0,
                                                  road_class="moon"))
    assert urban.score == unknown.score  # unknown road class falls back to the urban factor


def test_safety_night_share_clamped_and_zero_assigned_hours_safe():
    a = compute_safety_score(DriverExposure(harsh_brake_count=6.0, km=1000.0, night_km=1000.0, assigned_hours=50.0))
    b = compute_safety_score(DriverExposure(harsh_brake_count=6.0, km=1000.0, night_km=2000.0, assigned_hours=50.0))
    assert a.score == b.score  # night multiplier caps at 1.5x
    z = compute_safety_score(DriverExposure(harsh_brake_count=1.0, km=500.0, assigned_hours=0.0))
    assert 0.0 <= z.score <= 100.0 and z.per_100km["idle_share"] == 0.0


def test_coaching_top_n_and_idle_lever():
    rates = {"harsh_brake": 0.1, "harsh_accel": 0.1, "overspeed_min": 0.1, "idle_share": 0.9}
    med = {"harsh_brake": 0.1, "harsh_accel": 0.1, "overspeed_min": 0.1, "idle_share": 0.1}
    (lever,) = coaching_levers(rates, med, top_n=1)
    assert lever.lever == "idle_share" and "fleet p50" in lever.message
    assert len(coaching_levers(rates, med)) == 1  # only the positive gap surfaces


# ---------------------------------------------------------------- signatures
def test_signatures_deterministic():
    t = _day_table(["V1", "V1", "V2"], [1, 7, 13], ["HARSH_BRAKE", "POSITION", "POSITION"])
    a, b = build_signatures(t, DAY), build_signatures(t, DAY)
    assert (a["vin"] == b["vin"]).all()
    assert np.array_equal(a["matrix"], b["matrix"])  # byte-identical rebuild
    assert a["matrix"].shape == (2, DIM)


def test_knn_edges():
    rng = np.random.default_rng(2)
    corpus = rng.normal(size=(3, DIM))
    q = rng.normal(size=DIM)
    idx, sims = knn_search(q, corpus, k=0)
    assert len(idx) == 0 and len(sims) == 0
    idx, _ = knn_search(q, corpus, k=99)
    assert len(idx) == 3  # k larger than the corpus returns everything
    idx, _ = knn_search(np.zeros(3), corpus, k=2)
    assert len(idx) == 0  # wrong query dim is rejected, not broadcast
    idx, _ = knn_search(q, np.zeros((0, DIM)), k=2)
    assert len(idx) == 0
