"""4-feature model matrix for the RiskScorer contract (spec 10.1).

Contract (do not break): ``services/processor`` ``Features.vector()`` returns exactly
``[dtc_count, harsh_count, idle_ratio, km_since_service]`` and ``RiskScorer`` calls
``booster.predict([vector])``. ``MODEL_FEATURES`` below is that order; the saved
LightGBM booster is trained on exactly these 4 columns.

Leak discipline: features use ONLY telemetry rows with ``ts_ms`` strictly before
``prediction_ts_ms``. Label machinery (wear, breakdown hazard, realised-breakdown
labels) is NOT reimplemented here — it is imported from ``fs_simulator``.
``build_4features`` raises ``LeakageError`` if any used row violates the bound, so a
training run fails instead of silently leaking.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

import numpy as np
import pyarrow as pa
import structlog
from fs_simulator.features import assert_no_leakage, maintenance_labels
from fs_simulator.groundtruth import BREAKDOWN, GroundTruth
from fs_simulator.wear import (  # noqa: F401  (re-exported for train/eval)
    WearModel,
    dtc_rate_per_hour,
    hazard_per_day,
)

log = structlog.get_logger("fs_ml.features")

MODEL_FEATURES = ("dtc_count", "harsh_count", "idle_ratio", "km_since_service")
WINDOW_DAYS_DEFAULT = 7.0
DAY_MS = 86_400_000
IDLE_STATE = 2  # matches fs_simulator STATE_NAMES: parked/driving/idling/charging
HARSH_EVTS = ("HARSH_BRAKE", "HARSH_ACCEL")
DTC_CODES = ("P0301", "P0302", "P0171", "P0420", "P0442", "P0300", "P0217", "P0524")
WMI_POOL = ("1HG", "2HG", "JHM", "WBA")

TELEMETRY_COLS = ("vin", "ts_ms", "dtc", "evt", "state", "odo_km", "soh_pct", "speed_kmh")


def _col_str(t: pa.Table, name: str) -> np.ndarray:
    c = t[name]
    c = c.combine_chunks() if isinstance(c, pa.ChunkedArray) else c
    if pa.types.is_dictionary(c.type):
        c = c.dictionary_decode()
    return np.array(c.to_pylist(), dtype=object)


def _col_f64(t: pa.Table, name: str, fill: float) -> np.ndarray:
    c = t[name]
    c = c.combine_chunks() if isinstance(c, pa.ChunkedArray) else c
    if pa.types.is_dictionary(c.type):
        c = c.dictionary_decode()
    return np.array([fill if v is None else float(v) for v in c.to_pylist()], dtype=np.float64)


def build_4features(rows: pa.Table, prediction_ts_ms: int, window_days: float = WINDOW_DAYS_DEFAULT) -> dict[str, np.ndarray]:
    """Per-VIN 4-vector from lake telemetry strictly before ``prediction_ts_ms``.

    Returns dict with ``vin`` (object array) plus one float array per MODEL_FEATURES name:
    dtc_count = rows with a DTC set; harsh_count = HARSH_BRAKE/ACCEL rows;
    idle_ratio = share of rows with state == idling; km_since_service = km driven in
    the window (max-min odo; proxy when service history is unavailable).
    """
    for c in ("vin", "ts_ms", "dtc", "evt", "state", "odo_km"):
        if c not in rows.column_names:
            raise KeyError(f"telemetry table missing column {c!r}")
    ts = rows["ts_ms"].to_numpy()
    lo = prediction_ts_ms - int(window_days * DAY_MS)
    sub = rows.filter(pa.array((ts < prediction_ts_ms) & (ts >= lo)))
    if sub.num_rows == 0:
        empty = np.zeros(0, dtype=np.float64)
        return {"vin": np.zeros(0, dtype=object), **{k: empty.copy() for k in MODEL_FEATURES}}
    assert_no_leakage(int(sub["ts_ms"].to_numpy().max()), prediction_ts_ms)
    vin = _col_str(sub, "vin")
    vins, inv = np.unique(vin, return_inverse=True)
    k = len(vins)
    dtc = _col_str(sub, "dtc")
    has_dtc = np.array([d is not None for d in dtc])
    evt = _col_str(sub, "evt")
    harsh = np.isin(evt, HARSH_EVTS)
    state = _col_f64(sub, "state", -1.0)
    odo = _col_f64(sub, "odo_km", 0.0)
    cnt = np.maximum(np.bincount(inv, minlength=k).astype(float), 1.0)
    omax = np.full(k, -np.inf)
    omin = np.full(k, np.inf)
    np.maximum.at(omax, inv, odo)
    np.minimum.at(omin, inv, odo)
    return {
        "vin": vins,
        "dtc_count": np.bincount(inv, weights=has_dtc, minlength=k).astype(float),
        "harsh_count": np.bincount(inv, weights=harsh, minlength=k).astype(float),
        "idle_ratio": np.bincount(inv, weights=(state == IDLE_STATE), minlength=k) / cnt,
        "km_since_service": np.maximum(omax - omin, 0.0),
    }


@dataclass(slots=True)
class TrainingFrame:
    X: np.ndarray  # (N, 4) float64, column order == MODEL_FEATURES
    y: np.ndarray  # (N,) bool
    vin: np.ndarray  # (N,) object
    pred_ts: np.ndarray  # (N,) int64


def build_training_frame(
    telemetry: pa.Table,
    incidents: pa.Table,
    prediction_ts_ms: list[int],
    horizon_days: float = 7.0,
    window_days: float = WINDOW_DAYS_DEFAULT,
) -> TrainingFrame:
    """Join windowed 4-features (leak-checked) with realised-breakdown labels.

    Labels come from ``fs_simulator.features.maintenance_labels`` over BREAKDOWN
    incidents only; ``will_fail`` = breakdown in (pred_ts, pred_ts + horizon].
    """
    kinds = incidents["kind"].to_pylist()
    bd = incidents.filter(pa.array([kk == BREAKDOWN for kk in kinds]))
    bd_vins = np.array(bd["vin"].to_pylist(), dtype=object)
    bd_vins = bd_vins[bd_vins != "*"]
    bt_all = bd.filter(pa.array([v != "*" for v in bd["vin"].to_pylist()]))["start_ts_ms"].to_numpy()
    tel_vins = np.array(_col_str(telemetry, "vin"), dtype=object)
    all_vins = np.unique(np.concatenate([np.unique(tel_vins), np.unique(bd_vins)]) if len(bd_vins) else np.unique(tel_vins))
    index = {v: i for i, v in enumerate(all_vins.tolist())}
    bv = np.array([index[v] for v in bd_vins.tolist()], dtype=np.int64)
    labels = maintenance_labels(all_vins, bv, np.asarray(bt_all, dtype=np.int64), sorted(prediction_ts_ms), horizon_days)
    lab_vin = np.array(labels["vin"].to_pylist(), dtype=object)
    lab_pred = labels["prediction_ts_ms"].to_numpy()
    lab_y = labels["will_fail"].to_numpy(zero_copy_only=False).astype(bool)
    lab = {(v, int(p)): bool(w) for v, p, w in zip(lab_vin, lab_pred, lab_y, strict=True)}
    xs, ys, vs, ps = [], [], [], []
    for p in sorted(prediction_ts_ms):
        f = build_4features(telemetry, int(p), window_days)  # raises LeakageError on violation
        if len(f["vin"]) == 0:
            continue
        xs.append(np.stack([f[name] for name in MODEL_FEATURES], axis=1))
        ys.append(np.array([lab.get((v, int(p)), False) for v in f["vin"].tolist()]))
        vs.append(f["vin"])
        ps.append(np.full(len(f["vin"]), int(p), dtype=np.int64))
    if not xs:
        return TrainingFrame(np.zeros((0, 4)), np.zeros(0, dtype=bool), np.zeros(0, dtype=object), np.zeros(0, dtype=np.int64))
    frame = TrainingFrame(np.concatenate(xs), np.concatenate(ys), np.concatenate(vs), np.concatenate(ps))
    log.info("training_frame", rows=int(frame.X.shape[0]), positives=int(frame.y.sum()), preds=len(prediction_ts_ms))
    return frame


def temporal_split(pred_ts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Train on all but the latest prediction time; test on the latest (never random rows)."""
    if len(pred_ts) == 0:
        return np.zeros(0, dtype=bool), np.zeros(0, dtype=bool)
    cutoff = int(pred_ts.max())
    train = pred_ts < cutoff
    test = pred_ts >= cutoff
    if not train.any():  # single prediction time: fall back to first-half/second-half by order
        order = np.argsort(pred_ts, kind="stable")
        train = np.zeros(len(pred_ts), dtype=bool)
        train[order[: len(order) // 2]] = True
        test = ~train
    return train, test


def vin_split(vins: np.ndarray, test_mod: int = 5, test_rem: int = 4) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic by-VIN held-out split: sorted VINs, every ``test_mod``-th VIN is test."""
    order = np.argsort(vins.astype(str), kind="stable")
    rank = np.empty(len(vins), dtype=np.int64)
    rank[order] = np.arange(len(vins))
    test = (rank % test_mod) == test_rem
    return ~test, test


def vin_hash_split(vins: np.ndarray, test_share: float = 0.2) -> tuple[np.ndarray, np.ndarray]:
    """Hash-based by-VIN split (stable when the fleet grows)."""
    test = np.array([int(hashlib.md5(str(v).encode()).hexdigest(), 16) % 1000 <  # noqa: S324 - deterministic VIN sharding, not security
                       test_share * 1000 for v in vins])
    return ~test, test


@dataclass(slots=True)
class SyntheticLake:
    telemetry: pa.Table
    incidents: pa.Table
    vins: np.ndarray
    prediction_ts_ms: list[int]
    horizon_days: float


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def generate_synthetic_lake(
    n_vehicles: int = 2000,
    seed: int = 7,
    history_days: int = 14,
    horizon_days: float = 7.0,
    pred_days: tuple[int, ...] = (7, 14),
    warmup_days: int = 60,
    noise_level: int = 0,
    dtc_drop: tuple[float, ...] = (0.0, 0.10, 0.25),
    spurious_harsh: tuple[float, ...] = (0.0, 0.01, 0.03),
) -> SyntheticLake:
    """Small fast training dataset reusing the simulator wear/label modules.

    Wear evolves via ``WearModel`` (hidden state, never exposed as a feature);
    DTC/harsh/idle/odo observables are stochastic functions of ``w``; breakdowns
    come from the wear hazard via ``WearModel.sample_breakdowns`` and are recorded
    in a ``GroundTruth`` table. Labels are derived later with ``maintenance_labels``.

    ``noise_level`` in {0, 1, 2} is the robustness-curve knob: higher levels drop
    more DTC rows and inject spurious harsh events (transport/sensor noise).
    """
    from fscore.vin import make_vin

    rng = np.random.default_rng(seed)
    vrng = random.Random(seed)  # noqa: S311 - reproducible synthetic VINs, not secrets
    vins = np.array([make_vin(WMI_POOL[i % len(WMI_POOL)], vrng) for i in range(n_vehicles)], dtype=object)
    has_battery = rng.random(n_vehicles) < 0.4
    wear = WearModel(rng, n_vehicles, has_battery)
    usage = rng.lognormal(0.0, 0.3, n_vehicles)
    for _ in range(warmup_days):
        wear.advance_day(rng, usage)
    anchor_ms = 1_757_000_000_000  # fixed synthetic epoch; keeps splits deterministic
    day_ms = DAY_MS
    total_days = history_days + int(horizon_days) + 1
    odo = rng.uniform(5_000.0, 80_000.0, n_vehicles)
    drop_p = dtc_drop[noise_level]
    spur_p = spurious_harsh[noise_level]
    gt = GroundTruth(vins)
    cols: dict[str, list] = {c: [] for c in TELEMETRY_COLS}
    alive = np.ones(n_vehicles, dtype=bool)
    for day in range(total_days):
        day_start = anchor_ms + day * day_ms
        w = wear.w.copy()
        if day < history_days:
            rate = dtc_rate_per_hour(w)
            for h in range(24):
                ts = int(day_start + h * 3_600_000)
                p_dtc = 1.0 - np.exp(-rate)
                has_dtc = rng.random(n_vehicles) < p_dtc
                if drop_p:
                    has_dtc &= rng.random(n_vehicles) >= drop_p
                p_harsh = 0.015 + 0.10 * _sigmoid((w - 1.0) / 0.15)
                harsh = (rng.random(n_vehicles) < p_harsh) | (rng.random(n_vehicles) < spur_p)
                p_idle = np.clip(0.10 + 0.05 * w, 0.0, 0.4)
                r = rng.random(n_vehicles)
                state = np.where(r < p_idle, 2, np.where(r < p_idle + 0.45, 1, 0)).astype(np.int8)
                speed = np.where(state == 1, np.clip(rng.normal(45.0, 15.0, n_vehicles), 0, 120), 0.0)
                odo = odo + np.where(state == 1, speed, 0.0)
                dtc = np.where(has_dtc, rng.choice(np.array(DTC_CODES, dtype=object), n_vehicles), None)
                evt = np.where(has_dtc, "DTC_SET", np.where(harsh, rng.choice(np.array(["HARSH_BRAKE", "HARSH_ACCEL"]), n_vehicles), "POSITION"))
                soh = wear.soh.copy()
                for i in range(n_vehicles):
                    cols["vin"].append(vins[i])
                    cols["ts_ms"].append(ts)
                    cols["dtc"].append(None if dtc[i] is None else str(dtc[i]))
                    cols["evt"].append(str(evt[i]))
                    cols["state"].append(int(state[i]))
                    cols["odo_km"].append(float(odo[i]))
                    cols["soh_pct"].append(None if np.isnan(soh[i]) else float(soh[i]))
                    cols["speed_kmh"].append(float(speed[i]))
        mask, delay = wear.sample_breakdowns(rng, alive)
        if mask.any():
            idx = np.flatnonzero(mask)
            ts = np.minimum(day_start + delay[mask].astype(np.int64) * 1000, day_start + day_ms - 1)
            gt.add(BREAKDOWN, idx, ts, ts, note="synthetic wear breakdown")
            wear.repair(mask)
        wear.advance_day(rng, usage)
    telemetry = pa.table({
        "vin": cols["vin"], "ts_ms": pa.array(cols["ts_ms"], type=pa.int64()),
        "dtc": pa.array(cols["dtc"], type=pa.string()), "evt": cols["evt"],
        "state": pa.array(cols["state"], type=pa.int8()), "odo_km": pa.array(cols["odo_km"], type=pa.float32()),
        "soh_pct": pa.array(cols["soh_pct"], type=pa.float32()), "speed_kmh": pa.array(cols["speed_kmh"], type=pa.float32()),
    })
    preds = [int(anchor_ms + d * day_ms) for d in pred_days if d <= history_days]
    lake = SyntheticLake(telemetry, gt.table(), vins, preds, float(horizon_days))
    n_bd = sum(1 for kk in lake.incidents["kind"].to_pylist() if kk == BREAKDOWN)
    log.info("synthetic_lake", vehicles=n_vehicles, rows=telemetry.num_rows, breakdowns=n_bd, noise=noise_level)
    return lake
