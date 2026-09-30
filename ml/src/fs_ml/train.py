"""LightGBM training + isotonic calibration + baselines (spec 10.1).

The deployed artifact ``model.txt`` is a LightGBM Booster trained on exactly the
4 ``MODEL_FEATURES`` columns in order, so ``RiskScorer(model_path=...)`` loads it
with zero glue code. The isotonic calibrator (``calibrator.json``) is applied in
the batch/decision path where expected-$ math needs calibrated probabilities;
the raw booster already outputs probabilities for the streaming scorer.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import structlog
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

from . import evaluate as ev
from .features import (
    MODEL_FEATURES,
    TrainingFrame,
    build_training_frame,
    generate_synthetic_lake,
    temporal_split,
    vin_split,
)

log = structlog.get_logger("fs_ml.train")

BASELINE_HIGH = 0.9  # must match processor rule baseline
BASELINE_LOW = 0.15
CALIBRATOR_EPS = 1e-9

LGB_PARAMS: dict = {
    "objective": "binary",
    "metric": "average_precision",
    "num_leaves": 15,
    "min_data_in_leaf": 50,
    "learning_rate": 0.05,
    "feature_fraction": 1.0,
    "bagging_fraction": 1.0,
    "bagging_freq": 0,
    "verbosity": -1,
}


def rule_scores(X: np.ndarray) -> np.ndarray:
    """Baseline 1: ``active DTC -> high`` (spec 8.6 / 10.1)."""
    return np.where(X[:, 0] > 0, BASELINE_HIGH, BASELINE_LOW)


def train_lightgbm(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    seed: int = 7,
    num_rounds: int = 300,
) -> lgb.Booster:
    params = {**LGB_PARAMS, "seed": seed, "deterministic": True}
    pos = int(y_train.sum())
    if 0 < pos < len(y_train):
        params["scale_pos_weight"] = (len(y_train) - pos) / pos
    dtrain = lgb.Dataset(X_train, label=y_train.astype(float), feature_name=list(MODEL_FEATURES))
    dval = lgb.Dataset(X_val, label=y_val.astype(float), reference=dtrain)
    booster = lgb.train(
        params, dtrain, num_boost_round=num_rounds, valid_sets=[dval],
        callbacks=[lgb.early_stopping(50, verbose=False)],
    )
    return booster


def train_logreg(X_train: np.ndarray, y_train: np.ndarray) -> LogisticRegression:
    clf = LogisticRegression(max_iter=2000)
    clf.fit(X_train, y_train)
    return clf


def fit_calibrator(p_val: np.ndarray, y_val: np.ndarray) -> dict[str, list[float]]:
    """Isotonic calibrator stored as plain x/y arrays (applied with np.interp)."""
    p = np.clip(np.asarray(p_val, dtype=float), 0.0, 1.0)
    ir = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    ir.fit(p, np.asarray(y_val, dtype=float))
    xs = np.linspace(0.0, 1.0, 101)
    return {"x": [float(v) for v in xs], "y": [float(v) for v in ir.predict(xs)]}


def apply_calibrator(cal: dict[str, list[float]], p: np.ndarray) -> np.ndarray:
    return np.interp(np.clip(np.asarray(p, dtype=float), 0.0, 1.0), np.array(cal["x"]), np.array(cal["y"]))


def booster_predict(booster: lgb.Booster, X: np.ndarray) -> np.ndarray:
    if X.shape[0] == 0:
        return np.zeros(0)
    return np.asarray(booster.predict(X), dtype=float)


def save_artifacts(out_dir: Path, booster: lgb.Booster, calibrator: dict) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / "model.txt"
    cal_path = out_dir / "calibrator.json"
    booster.save_model(str(model_path))
    cal_path.write_text(json.dumps({"features": list(MODEL_FEATURES), "isotonic": calibrator}), encoding="utf-8")
    log.info("artifacts_saved", model=str(model_path), calibrator=str(cal_path))
    return {"model": str(model_path), "calibrator": str(cal_path)}


def load_calibrator(path: str | Path) -> dict[str, list[float]]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["isotonic"]


def run_training(
    n_vehicles: int = 2000,
    seed: int = 7,
    out_dir: Path | str = Path("ml/artifacts"),
    k_capacity: int = 50,
    c_breakdown: float = 2500.0,
    c_inspection: float = 150.0,
) -> dict:
    """Full pipeline: synthetic data -> frame -> both splits -> train -> evaluate -> save."""
    out = Path(out_dir)
    lake = generate_synthetic_lake(n_vehicles=n_vehicles, seed=seed, noise_level=0)
    frame: TrainingFrame = build_training_frame(lake.telemetry, lake.incidents, lake.prediction_ts_ms, lake.horizon_days)
    splits = {
        "temporal": temporal_split(frame.pred_ts),
        "by_vin": vin_split(frame.vin),
    }
    report: dict = {
        "n_vehicles": n_vehicles, "seed": seed, "rows": int(frame.X.shape[0]),
        "positives": int(frame.y.sum()), "features": list(MODEL_FEATURES),
        "k_capacity": k_capacity, "c_breakdown": c_breakdown, "c_inspection": c_inspection,
    }
    trained: dict[str, object] = {}
    for name, (tr, te) in splits.items():
        Xtr, ytr, Xte, yte = frame.X[tr], frame.y[tr], frame.X[te], frame.y[te]
        # carve a validation slice from train for early stopping + calibration
        cut = int(len(Xtr) * 0.8)
        booster = train_lightgbm(Xtr[:cut], ytr[:cut], Xtr[cut:] if len(Xtr) - cut else Xte, ytr[cut:] if len(Xtr) - cut else yte, seed)
        p_val = booster_predict(booster, Xtr[cut:] if len(Xtr) - cut else Xte)
        cal = fit_calibrator(p_val, ytr[cut:] if len(Xtr) - cut else yte)
        logreg = train_logreg(Xtr, ytr)
        preds = {
            "lgbm_calibrated": apply_calibrator(cal, booster_predict(booster, Xte)),
            "lgbm_raw": booster_predict(booster, Xte),
            "rule": rule_scores(Xte),
            "logreg": logreg.predict_proba(Xte)[:, 1] if len(np.unique(ytr)) > 1 else rule_scores(Xte),
        }
        report[name] = {m: ev.evaluate_predictions(yte, p, k_capacity, c_breakdown, c_inspection) for m, p in preds.items()}
        report[name]["n_train"], report[name]["n_test"] = int(tr.sum()), int(te.sum())
        if name == "temporal":
            trained = {"booster": booster, "cal": cal}
    report["robustness"] = ev.robustness_curve(
        n_vehicles=min(n_vehicles, 1500), seed=seed, k_capacity=k_capacity,
        c_breakdown=c_breakdown, c_inspection=c_inspection,
    )
    booster, cal = trained["booster"], trained["cal"]  # type: ignore[index]
    paths = save_artifacts(out, booster, cal)  # type: ignore[arg-type]
    report.update(paths)
    (out / "metrics.json").write_text(json.dumps(report, indent=2, default=float), encoding="utf-8")
    log.info("training_done", **{kk: vv for kk, vv in report.items() if isinstance(vv, (int, float, str))})
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Train FleetSentinel maintenance-risk model")
    ap.add_argument("--n-vehicles", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=Path("ml/artifacts"))
    ap.add_argument("--k", type=int, default=50)
    args = ap.parse_args()
    report = run_training(n_vehicles=args.n_vehicles, seed=args.seed, out_dir=args.out, k_capacity=args.k)
    print(json.dumps({kk: vv for kk, vv in report.items() if kk not in ("temporal", "by_vin", "robustness")}, indent=2))


if __name__ == "__main__":
    main()
