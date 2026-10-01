"""Leak-safe feature extraction and maintenance labels (spec 6.1 / 10.1).

Features use ONLY telemetry rows with ts_ms strictly before the prediction time; the hidden wear state is never read.
Labels come from realised breakdown incidents in the ground truth (a hazard process on the hidden wear w).
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa

DAY_MS = 86_400_000
FEATURE_NAMES = ("n_rows", "dtc_rows", "dtc_distinct", "critical_dtc_rows", "soh_last", "soh_slope_per_day",
                 "harsh_events", "km_driven", "idle_share", "mean_speed_moving", "hours_since_last_dtc")
CRITICAL = ("P0217", "P0524", "P0AA6", "P0A0D", "P0AFA", "B0001", "U0100", "P0300")


class LeakageError(AssertionError):
    pass


def assert_no_leakage(max_ts_ms_used: int, prediction_ts_ms: int) -> None:
    if max_ts_ms_used >= prediction_ts_ms:
        raise LeakageError(f"feature uses ts {max_ts_ms_used} >= prediction_ts {prediction_ts_ms}")


def _col(t: pa.Table, name: str) -> pa.Array:
    c = t[name]
    c = c.combine_chunks() if isinstance(c, pa.ChunkedArray) else c
    return c.dictionary_decode() if pa.types.is_dictionary(c.type) else c


def build_features(rows: pa.Table, prediction_ts_ms: int, window_days: float = 7.0) -> dict[str, np.ndarray]:
    """rows: lake telemetry table (vin, ts_ms, soh_pct, dtc, speed_kmh, odo_km, evt, state). Returns per-VIN features."""
    ts_all = rows["ts_ms"].to_numpy()
    m = (ts_all < prediction_ts_ms) & (ts_all >= prediction_ts_ms - int(window_days * DAY_MS))   # STRICT upper bound
    sub = rows.filter(pa.array(m))
    if sub.num_rows:
        assert_no_leakage(int(sub["ts_ms"].to_numpy().max()), prediction_ts_ms)
    vins, inv = np.unique(np.array(_col(sub, "vin").to_pylist(), dtype=object), return_inverse=True)
    k = len(vins)
    if k == 0:  # empty pre-prediction window: valid input (new vehicle), return empty frame, not IndexError
        zf, zo = np.zeros(0), np.zeros(0, dtype=object)
        return {"vin": zo, "n_rows": zf, "dtc_rows": zf, "dtc_distinct": zf, "critical_dtc_rows": zf,
                "soh_last": zf, "soh_slope_per_day": zf, "harsh_events": zf, "km_driven": zf,
                "idle_share": zf, "mean_speed_moving": zf, "hours_since_last_dtc": zf}
    t = sub["ts_ms"].to_numpy().astype(float)
    dtc = np.array(_col(sub, "dtc").to_pylist(), dtype=object)
    has_dtc = np.array([d is not None for d in dtc])
    crit = np.isin(dtc, CRITICAL)
    soh = _col(sub, "soh_pct").fill_null(float("nan")).to_numpy(zero_copy_only=False).astype(float)
    speed = _col(sub, "speed_kmh").fill_null(0.0).to_numpy(zero_copy_only=False).astype(float)
    odo = _col(sub, "odo_km").fill_null(0.0).to_numpy(zero_copy_only=False).astype(float)
    evt = np.array(_col(sub, "evt").to_pylist(), dtype=object)
    state = sub["state"].to_numpy()
    cnt = np.maximum(np.bincount(inv, minlength=k).astype(float), 1.0)
    moving = speed > 1.0
    ok = ~np.isnan(soh)
    x = (t - t.min()) / DAY_MS if len(t) else t
    n = np.bincount(inv[ok], minlength=k).astype(float)
    sx, sy = np.bincount(inv[ok], weights=x[ok], minlength=k), np.bincount(inv[ok], weights=soh[ok], minlength=k)
    sxy, sxx = np.bincount(inv[ok], weights=(x * soh)[ok], minlength=k), np.bincount(inv[ok], weights=(x * x)[ok], minlength=k)
    den = n * sxx - sx * sx
    slope = np.where((n >= 2) & (den > 1e-9), (n * sxy - sx * sy) / np.where(den > 1e-9, den, 1.0), np.nan)
    order = np.lexsort((t, inv))
    last = np.full(k, np.nan)
    km_time_ordered = np.zeros(k)
    if len(order):
        lastpos = np.r_[np.flatnonzero(np.diff(inv[order])), len(order) - 1]
        firstpos = np.r_[0, np.flatnonzero(np.diff(inv[order])) + 1]
        odo_sorted = odo[order]
        # Time-ordered last-minus-first, clamped: an odometer rollback must not inflate km.
        km_time_ordered[inv[order][lastpos]] = np.maximum(
            odo_sorted[lastpos] - odo_sorted[firstpos], 0.0)
        soh_sorted = soh[order]
        last[inv[order][lastpos]] = soh_sorted[lastpos]
    last_dtc_t = np.full(k, -np.inf)
    np.maximum.at(last_dtc_t, inv[has_dtc], t[has_dtc])
    pairs = np.unique(np.stack([inv[has_dtc], np.array([hash(d) for d in dtc[has_dtc]])], axis=1), axis=0) if has_dtc.any() else np.zeros((0, 2))
    return {
        "vin": vins, "n_rows": cnt, "dtc_rows": np.bincount(inv, weights=has_dtc, minlength=k), "dtc_distinct":
        np.bincount(pairs[:, 0].astype(int), minlength=k).astype(float) if len(pairs) else np.zeros(k),
        "critical_dtc_rows": np.bincount(inv, weights=crit, minlength=k), "soh_last": last, "soh_slope_per_day": slope,
        "harsh_events": np.bincount(inv, weights=(evt == "HARSH_BRAKE") | (evt == "HARSH_ACCEL"), minlength=k),
        "km_driven": km_time_ordered, "idle_share": np.bincount(inv, weights=(state == 2), minlength=k) / cnt,
        "mean_speed_moving": np.bincount(inv, weights=speed * moving, minlength=k) / np.maximum(np.bincount(inv, weights=moving, minlength=k), 1.0),
        "hours_since_last_dtc": np.where(np.isfinite(last_dtc_t), (prediction_ts_ms - last_dtc_t) / 3_600_000.0, np.nan),
    }


def maintenance_labels(vins: np.ndarray, breakdown_vin_idx: np.ndarray, breakdown_ts_ms: np.ndarray,
                       prediction_ts_ms: list[int], horizon_days: float = 7.0) -> pa.Table:
    """Label table (vin, prediction_ts_ms, horizon_days, will_fail, breakdown_ts_ms). will_fail = a breakdown in
    (prediction_ts, prediction_ts + horizon]. Derived only from realised breakdowns; wear w is never exposed."""
    h = int(horizon_days * DAY_MS)
    n = len(vins)
    cols: dict[str, list] = {"vin": [], "prediction_ts_ms": [], "horizon_days": [], "will_fail": [], "breakdown_ts_ms": []}
    order = np.argsort(breakdown_ts_ms, kind="stable")
    bv, bt = breakdown_vin_idx[order], breakdown_ts_ms[order]
    for p in prediction_ts_ms:
        nxt = np.full(n, -1, dtype=np.int64)
        sel = (bt > p) & (bt <= p + h)
        nxt[bv[sel][::-1]] = bt[sel][::-1]                    # earliest breakdown wins (assigned last)
        cols["vin"].append(vins)
        cols["prediction_ts_ms"].append(np.full(n, p, dtype=np.int64))
        cols["horizon_days"].append(np.full(n, horizon_days))
        cols["will_fail"].append(nxt >= 0)
        cols["breakdown_ts_ms"].append(nxt)
    return pa.table({"vin": np.concatenate(cols["vin"]).tolist(), "prediction_ts_ms": np.concatenate(cols["prediction_ts_ms"]),
                     "horizon_days": np.concatenate(cols["horizon_days"]), "will_fail": np.concatenate(cols["will_fail"]),
                     "breakdown_ts_ms": pa.array(np.concatenate(cols["breakdown_ts_ms"]),
                                                  mask=np.concatenate(cols["breakdown_ts_ms"]) < 0)})
