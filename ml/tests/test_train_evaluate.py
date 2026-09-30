"""Tests for training, calibration, baselines, and evaluation metrics."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fs_ml import evaluate as ev
from fs_ml.features import build_training_frame, generate_synthetic_lake, temporal_split, vin_split
from fs_ml.train import (
    apply_calibrator,
    booster_predict,
    fit_calibrator,
    load_calibrator,
    rule_scores,
    save_artifacts,
    train_lightgbm,
    train_logreg,
)


@pytest.fixture(scope="module")
def frame():
    lake = generate_synthetic_lake(n_vehicles=300, seed=21, history_days=14, warmup_days=10)
    return build_training_frame(lake.telemetry, lake.incidents, lake.prediction_ts_ms, lake.horizon_days)


def test_rule_baseline_matches_processor_constants():
    from fs_processor.domain.scoring import BASELINE_HIGH, BASELINE_LOW

    X = np.array([[0.0, 0, 0, 0], [2.0, 1, 0.1, 500.0]])
    assert rule_scores(X).tolist() == [BASELINE_LOW, BASELINE_HIGH]


def test_train_predict_roundtrip(frame):
    tr, te = temporal_split(frame.pred_ts)
    Xtr, ytr, Xte = frame.X[tr], frame.y[tr], frame.X[te]
    cut = max(int(len(Xtr) * 0.8), 1)
    booster = train_lightgbm(Xtr[:cut], ytr[:cut], Xtr[cut:], ytr[cut:], seed=1)
    p = booster_predict(booster, Xte)
    assert p.shape == (int(te.sum()),)
    assert ((p >= 0.0) & (p <= 1.0)).all()


def test_calibrator_monotone_and_bounded(frame):
    tr, te = temporal_split(frame.pred_ts)
    Xtr, ytr, Xte = frame.X[tr], frame.y[tr], frame.X[te]
    cut = max(int(len(Xtr) * 0.8), 1)
    booster = train_lightgbm(Xtr[:cut], ytr[:cut], Xtr[cut:], ytr[cut:], seed=1)
    cal = fit_calibrator(booster_predict(booster, Xtr[cut:]), ytr[cut:])
    assert np.all(np.diff(np.array(cal["y"])) >= 0)  # isotonic => monotone
    pc = apply_calibrator(cal, booster_predict(booster, Xte))
    assert ((pc >= 0.0) & (pc <= 1.0)).all()


def test_save_load_artifacts(tmp_path: Path, frame):
    tr, _ = temporal_split(frame.pred_ts)
    Xtr, ytr = frame.X[tr], frame.y[tr]
    cut = max(int(len(Xtr) * 0.8), 1)
    booster = train_lightgbm(Xtr[:cut], ytr[:cut], Xtr[cut:], ytr[cut:], seed=1)
    cal = fit_calibrator(booster_predict(booster, Xtr[cut:]), ytr[cut:])
    paths = save_artifacts(tmp_path, booster, cal)
    assert Path(paths["model"]).exists() and Path(paths["calibrator"]).exists()
    assert load_calibrator(paths["calibrator"]) == cal
    # reload through the real RiskScorer: probabilities, not fallback
    from fs_processor.domain.scoring import BASELINE_VERSION, Features, RiskScorer

    scorer = RiskScorer(model_path=paths["model"])
    assert scorer.fallback is False
    s, v = scorer.score(Features(1, 2, 0.1, 5000.0), True)
    assert v != BASELINE_VERSION and 0.0 <= s <= 1.0


def test_metrics_sane(frame):
    tr, te = temporal_split(frame.pred_ts)
    yte = frame.y[te]
    p = rule_scores(frame.X[te])
    m = ev.evaluate_predictions(yte, p, k=20)
    assert 0.0 <= m["precision_at_k"] <= 1.0
    assert 0.0 <= m["brier"] <= 1.0
    assert m["savings"]["n_inspected"] <= 20
    cal = m["calibration"]
    assert len(cal["edges"]) == 11 and len(cal["count"]) == 10


def test_expected_savings_prefers_true_positives():
    y = np.array([1, 1, 0, 0])
    p = np.array([0.9, 0.8, 0.1, 0.05])
    s = ev.expected_savings(y, p, k=2, c_breakdown=2500.0, c_inspection=150.0)
    assert s["n_inspected"] == 2 and s["failures_caught"] == 2
    assert s["realized_usd"] == pytest.approx(2 * 2500.0 - 2 * 150.0)


def test_precision_at_k_edge_cases():
    assert np.isnan(ev.precision_at_k(np.zeros(0, dtype=bool), np.zeros(0), 10))
    assert np.isnan(ev.pr_auc(np.zeros(5, dtype=bool), np.zeros(5)))
    assert ev.precision_at_k(np.array([True, False]), np.array([0.9, 0.1]), 1) == 1.0


def test_model_pipeline_runs_on_both_splits(frame):
    for splitter in (temporal_split(frame.pred_ts), vin_split(frame.vin)):
        tr, te = splitter
        assert tr.any() and te.any()
        Xtr, ytr, Xte, yte = frame.X[tr], frame.y[tr], frame.X[te], frame.y[te]
        cut = max(int(len(Xtr) * 0.8), 1)
        booster = train_lightgbm(Xtr[:cut], ytr[:cut], Xtr[cut:], ytr[cut:], seed=2)
        cal = fit_calibrator(booster_predict(booster, Xtr[cut:]), ytr[cut:])
        pm = apply_calibrator(cal, booster_predict(booster, Xte))
        pr = rule_scores(Xte)
        mm = ev.evaluate_predictions(yte, pm, k=20)
        rm = ev.evaluate_predictions(yte, pr, k=20)
        assert 0.0 <= mm["brier"] <= 1.0 and 0.0 <= rm["brier"] <= 1.0


def test_logreg_baseline_runs(frame):
    tr, te = temporal_split(frame.pred_ts)
    clf = train_logreg(frame.X[tr], frame.y[tr])
    p = clf.predict_proba(frame.X[te])[:, 1]
    assert ((p >= 0) & (p <= 1)).all()


def test_robustness_curve_three_levels():
    curve = ev.robustness_curve(n_vehicles=120, seed=99)
    assert [c["noise_level"] for c in curve] == [0, 1, 2]
    for c in curve:
        assert 0.0 <= c["model"]["brier"] <= 1.0
        assert 0.0 <= c["rule"]["brier"] <= 1.0


def test_run_training_end_to_end(tmp_path):
    from fs_ml.train import run_training

    report = run_training(n_vehicles=150, seed=13, out_dir=tmp_path, k_capacity=10)
    assert report["features"] == ["dtc_count", "harsh_count", "idle_ratio", "km_since_service"]
    for split in ("temporal", "by_vin"):
        assert {"lgbm_calibrated", "lgbm_raw", "rule", "logreg"} <= set(report[split])
    assert len(report["robustness"]) == 3
    assert Path(report["model"]).exists()
    assert (tmp_path / "metrics.json").exists()


def test_booster_predict_empty():
    import lightgbm as lgb

    X = np.zeros((10, 4))
    y = np.array([0, 1] * 5, dtype=float)
    booster = lgb.train({"objective": "binary", "verbosity": -1}, lgb.Dataset(X, label=y), num_boost_round=2)
    assert booster_predict(booster, np.zeros((0, 4))).size == 0
