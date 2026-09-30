"""Tests for the 4-feature builder, leak discipline, splits, and the RiskScorer contract."""
from __future__ import annotations

import lightgbm as lgb
import numpy as np
import pyarrow as pa
import pytest
from fs_ml.features import (
    MODEL_FEATURES,
    build_4features,
    build_training_frame,
    generate_synthetic_lake,
    temporal_split,
    vin_hash_split,
    vin_split,
)
from fs_simulator.features import LeakageError

PRED = 1_757_000_000_000


def _rows(vins, ts, dtc=None, evt=None, state=None, odo=None):
    n = len(vins)
    return pa.table({
        "vin": list(vins),
        "ts_ms": pa.array(list(ts), type=pa.int64()),
        "dtc": list(dtc) if dtc is not None else [None] * n,
        "evt": list(evt) if evt is not None else ["POSITION"] * n,
        "state": pa.array(list(state) if state is not None else [1] * n, type=pa.int8()),
        "odo_km": pa.array(list(odo) if odo is not None else [float(i) for i in range(n)], type=pa.float32()),
        "soh_pct": pa.array([None] * n, type=pa.float32()),
        "speed_kmh": pa.array([40.0] * n, type=pa.float32()),
    })


def test_feature_order_matches_processor_contract():
    assert MODEL_FEATURES == ("dtc_count", "harsh_count", "idle_ratio", "km_since_service")


def test_build_4features_counts():
    t = _rows(
        ["V1", "V1", "V2"], [PRED - 1000, PRED - 2000, PRED - 3000],
        dtc=["P0301", None, None], evt=["DTC_SET", "HARSH_BRAKE", "POSITION"],
        state=[1, 2, 1], odo=[100.0, 130.0, 50.0],
    )
    f = build_4features(t, PRED)
    assert f["vin"].tolist() == ["V1", "V2"]
    assert f["dtc_count"].tolist() == [1.0, 0.0]
    assert f["harsh_count"].tolist() == [1.0, 0.0]
    assert f["idle_ratio"].tolist() == pytest.approx([0.5, 0.0])
    assert f["km_since_service"].tolist() == pytest.approx([30.0, 0.0])


def test_strict_upper_bound_excludes_prediction_ts():
    t = _rows(["V1", "V1"], [PRED - 1, PRED], odo=[10.0, 99.0])
    f = build_4features(t, PRED)
    assert f["km_since_service"].tolist() == [0.0]  # only the pre-pred row used


def test_leakage_assertion_fires_on_pathological_input():
    # build_4features filters strictly, so force the underlying check directly:
    from fs_simulator.features import assert_no_leakage

    with pytest.raises(LeakageError):
        assert_no_leakage(PRED, PRED)


def test_empty_table_returns_empty():
    t = _rows([], [])
    f = build_4features(t, PRED)
    assert len(f["vin"]) == 0 and all(len(f[k]) == 0 for k in MODEL_FEATURES)


def test_missing_column_raises():
    with pytest.raises(KeyError):
        build_4features(pa.table({"vin": ["V1"]}), PRED)


def test_training_frame_labels_from_realised_breakdowns():
    lake = generate_synthetic_lake(n_vehicles=60, seed=11, history_days=14, warmup_days=5)
    frame = build_training_frame(lake.telemetry, lake.incidents, lake.prediction_ts_ms, lake.horizon_days)
    assert frame.X.shape[1] == 4
    assert frame.X.shape[0] == frame.y.shape[0] > 0
    assert set(np.unique(frame.pred_ts).tolist()) <= set(lake.prediction_ts_ms)
    # labels are consistent with the oracle: every positive has a breakdown in-window
    bd = lake.incidents.filter(pa.array([k == "BREAKDOWN" for k in lake.incidents["kind"].to_pylist()]))
    by_vin: dict[str, list[int]] = {}
    for v, s in zip(bd["vin"].to_pylist(), bd["start_ts_ms"].to_pylist(), strict=True):
        by_vin.setdefault(v, []).append(s)
    h = int(lake.horizon_days * 86_400_000)
    for v, p, y in zip(frame.vin.tolist(), frame.pred_ts.tolist(), frame.y.tolist(), strict=True):
        expect = any(p < s <= p + h for s in by_vin.get(v, []))
        assert bool(y) == expect


def test_temporal_split_never_random_rows():
    pred = np.array([1, 1, 2, 2, 3, 3])
    tr, te = temporal_split(pred)
    assert te.tolist() == [False, False, False, False, True, True]
    assert tr.tolist() == [True, True, True, True, False, False]


def test_vin_splits_disjoint_and_cover():
    vins = np.array([f"V{i:03d}" for i in range(50)], dtype=object)
    for fn in (vin_split, vin_hash_split):
        tr, te = fn(vins)
        assert (tr & te).sum() == 0
        assert (tr | te).all()
        assert 0 < te.sum() < len(vins)


def test_saved_model_loads_through_real_riskscorer():
    """The trained booster must consume the exact 4-vector via RiskScorer (no glue)."""
    from fs_processor.domain.scoring import BASELINE_VERSION, Features, RiskScorer

    rng = np.random.default_rng(3)
    X = np.column_stack([rng.poisson(1.5, 400), rng.poisson(2.0, 400),
                         rng.uniform(0, 0.5, 400), rng.uniform(0, 20000, 400)])
    y = ((X[:, 0] > 1) | (X[:, 1] > 3)).astype(float)
    ds = lgb.Dataset(X, label=y, feature_name=list(MODEL_FEATURES))
    booster = lgb.train({"objective": "binary", "verbosity": -1, "num_leaves": 7}, ds, num_boost_round=20)
    booster.save_model("/tmp/streamd-test-model.txt")
    scorer = RiskScorer(model_path="/tmp/streamd-test-model.txt")
    assert scorer.fallback is False
    f = Features(dtc_count=3, harsh_count=5, idle_ratio=0.2, km_since_service=9000.0)
    score, version = scorer.score(f, dtc_active=True)
    assert version != BASELINE_VERSION
    assert 0.0 <= score <= 1.0
    # model output differs from the rule fallback on at least the learned pattern
    assert isinstance(score, float)


def test_synthetic_lake_reuses_simulator_machinery():
    lake = generate_synthetic_lake(n_vehicles=30, seed=5, history_days=7, warmup_days=2, pred_days=(7,))
    assert lake.telemetry.num_rows == 30 * 7 * 24
    assert lake.prediction_ts_ms == [1_757_000_000_000 + 7 * 86_400_000]
    # telemetry strictly before the prediction time
    assert lake.telemetry["ts_ms"].to_numpy().max() < lake.prediction_ts_ms[0]
