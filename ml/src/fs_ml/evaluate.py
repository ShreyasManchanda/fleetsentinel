"""Evaluation metrics for the maintenance-risk model (spec 10.1).

PR-AUC (failures are rare), precision@K (K = daily service-bay capacity),
Brier + calibration-plot data (probabilities drive dollar math), expected-$ saved
(the decision metric), and the robustness curve at 3 simulator noise levels.
"""
from __future__ import annotations

import numpy as np
import structlog
from sklearn.metrics import average_precision_score, brier_score_loss

log = structlog.get_logger("fs_ml.evaluate")


def pr_auc(y: np.ndarray, p: np.ndarray) -> float:
    y = np.asarray(y).astype(int)
    if y.sum() == 0 or y.sum() == len(y):
        return float("nan")
    with np.errstate(all="ignore"):
        return float(average_precision_score(y, np.asarray(p, dtype=float)))


def precision_at_k(y: np.ndarray, p: np.ndarray, k: int) -> float:
    y = np.asarray(y).astype(bool)
    order = np.argsort(np.asarray(p, dtype=float), kind="stable")[::-1][:k]
    if len(order) == 0:
        return float("nan")
    return float(y[order].mean())


def brier(y: np.ndarray, p: np.ndarray) -> float:
    if len(y) == 0:
        return float("nan")
    return float(brier_score_loss(np.asarray(y).astype(int), np.clip(np.asarray(p, dtype=float), 0.0, 1.0)))


def calibration_data(y: np.ndarray, p: np.ndarray, n_bins: int = 10) -> dict[str, list[float]]:
    """Uniform-bin calibration plot data: edges, mean predicted, observed fraction, counts."""
    y = np.asarray(y).astype(float)
    p = np.clip(np.asarray(p, dtype=float), 0.0, 1.0)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.minimum(np.digitize(p, edges[1:-1], right=False), n_bins - 1)
    mean_p, frac, cnt = [], [], []
    for b in range(n_bins):
        m = idx == b
        mean_p.append(float(p[m].mean()) if m.any() else float((edges[b] + edges[b + 1]) / 2))
        frac.append(float(y[m].mean()) if m.any() else float("nan"))
        cnt.append(int(m.sum()))
    return {"edges": [float(e) for e in edges], "mean_predicted": mean_p, "fraction_positive": frac, "count": cnt}


def expected_savings(
    y: np.ndarray, p: np.ndarray, k: int, c_breakdown: float, c_inspection: float
) -> dict[str, float]:
    """Decision metric: EV_i = p_i * C_breakdown - C_inspection; inspect top-K with EV > 0.

    Realized savings use the actual outcomes: sum over inspected of (y_i * C_bd - C_insp).
    """
    y = np.asarray(y).astype(float)
    p = np.asarray(p, dtype=float)
    ev = p * c_breakdown - c_inspection
    order = np.argsort(ev, kind="stable")[::-1]
    chosen = order[:k]
    chosen = chosen[ev[chosen] > 0]
    realized = float((y[chosen] * c_breakdown - c_inspection).sum()) if len(chosen) else 0.0
    caught = float(y[chosen].sum()) if len(chosen) else 0.0
    return {"realized_usd": realized, "n_inspected": float(len(chosen)), "failures_caught": caught}


def evaluate_predictions(
    y: np.ndarray, p: np.ndarray, k: int, c_breakdown: float = 2500.0, c_inspection: float = 150.0
) -> dict:
    return {
        "pr_auc": pr_auc(y, p),
        "precision_at_k": precision_at_k(y, p, k),
        "brier": brier(y, p),
        "calibration": calibration_data(y, p),
        "savings": expected_savings(y, p, k, c_breakdown, c_inspection),
        "n": int(len(np.asarray(y))),
        "positives": int(np.asarray(y).astype(bool).sum()),
    }


def robustness_curve(
    n_vehicles: int = 1500,
    seed: int = 7,
    k_capacity: int = 50,
    c_breakdown: float = 2500.0,
    c_inspection: float = 150.0,
) -> list[dict]:
    """Re-train + evaluate at 3 simulator noise levels; show model vs baseline degradation."""
    from .features import build_training_frame, generate_synthetic_lake, temporal_split
    from .train import apply_calibrator, booster_predict, fit_calibrator, rule_scores, train_lightgbm

    curve = []
    for level in (0, 1, 2):
        lake = generate_synthetic_lake(n_vehicles=n_vehicles, seed=seed + level, noise_level=level)
        frame = build_training_frame(lake.telemetry, lake.incidents, lake.prediction_ts_ms, lake.horizon_days)
        tr, te = temporal_split(frame.pred_ts)
        Xtr, ytr, Xte, yte = frame.X[tr], frame.y[tr], frame.X[te], frame.y[te]
        cut = int(len(Xtr) * 0.8)
        booster = train_lightgbm(Xtr[:cut], ytr[:cut], Xtr[cut:], ytr[cut:], seed + level)
        cal = fit_calibrator(booster_predict(booster, Xtr[cut:]), ytr[cut:])
        p_model = apply_calibrator(cal, booster_predict(booster, Xte))
        p_rule = rule_scores(Xte)
        curve.append({
            "noise_level": level,
            "model": evaluate_predictions(yte, p_model, k_capacity, c_breakdown, c_inspection),
            "rule": evaluate_predictions(yte, p_rule, k_capacity, c_breakdown, c_inspection),
        })
        log.info("robustness_point", level=level, model_pr=curve[-1]["model"]["pr_auc"], rule_pr=curve[-1]["rule"]["pr_auc"])
    return curve
