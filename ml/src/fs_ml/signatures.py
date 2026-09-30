"""32-dim vehicle-day telemetry signatures for the look-alike path (spec 10.4).

Stream C owns pgvector storage/query; this module delivers the vector builder
plus a brute-force cosine kNN reference implementation used in tests.

Signature: each vehicle-day is split into four 6-hour buckets; per bucket 8
signals (dtc_count, harsh_brake, harsh_accel, idle_ratio, km, mean_speed_moving,
night_share, overspeed_events). 4 x 8 = 32 dims, L2-normalized (zero days stay
zero). Cosine similarity on these vectors is the look-alike score.
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa

DIM = 32
N_BUCKETS = 4
BUCKET_H_MS = 6 * 3_600_000
NIGHT_START_H = 22
NIGHT_END_H = 6


def _str_col(t: pa.Table, name: str, default: str = "") -> np.ndarray:
    c = t[name]
    c = c.combine_chunks() if isinstance(c, pa.ChunkedArray) else c
    if pa.types.is_dictionary(c.type):
        c = c.dictionary_decode()
    return np.array([default if v is None else str(v) for v in c.to_pylist()], dtype=object)


def _f64_col(t: pa.Table, name: str, fill: float) -> np.ndarray:
    c = t[name]
    c = c.combine_chunks() if isinstance(c, pa.ChunkedArray) else c
    if pa.types.is_dictionary(c.type):
        c = c.dictionary_decode()
    return np.array([fill if v is None else float(v) for v in c.to_pylist()], dtype=np.float64)


def build_signatures(day_rows: pa.Table, day_start_ms: int) -> dict[str, np.ndarray]:
    """Build one L2-normalized 32-dim signature per VIN present in ``day_rows``.

    Rows should carry ``vin, ts_ms, dtc, evt, state, speed_kmh, odo_km``; rows
    outside ``[day_start_ms, day_start_ms + 24h)`` are ignored.
    Returns ``{"vin": (N,) object, "matrix": (N, 32) float64}``.
    """
    ts = day_rows["ts_ms"].to_numpy().astype(np.int64)
    keep = (ts >= day_start_ms) & (ts < day_start_ms + 24 * 3_600_000)
    sub = day_rows.filter(pa.array(keep))
    if sub.num_rows == 0:
        return {"vin": np.zeros(0, dtype=object), "matrix": np.zeros((0, DIM))}
    vin = _str_col(sub, "vin")
    vins, inv = np.unique(vin, return_inverse=True)
    n = len(vins)
    ts = sub["ts_ms"].to_numpy().astype(np.int64)
    bucket = np.minimum((ts - day_start_ms) // BUCKET_H_MS, N_BUCKETS - 1)
    dtc = _str_col(sub, "dtc")
    has_dtc = np.array([len(d) > 0 for d in dtc])
    evt = _str_col(sub, "evt")
    is_hb = evt == "HARSH_BRAKE"
    is_ha = evt == "HARSH_ACCEL"
    is_over = evt == "OVERSPEED"
    state = _f64_col(sub, "state", -1.0)
    speed = _f64_col(sub, "speed_kmh", 0.0)
    odo = _f64_col(sub, "odo_km", 0.0)
    hour = ((ts // 3_600_000) % 24).astype(int)
    night = (hour >= NIGHT_START_H) | (hour < NIGHT_END_H)
    moving = speed > 1.0
    mat = np.zeros((n, DIM))
    for b in range(N_BUCKETS):
        m = bucket == b
        if not m.any():
            continue
        ii = inv[m]
        cnt = np.maximum(np.bincount(ii, minlength=n).astype(float), 1.0)
        o = np.zeros((n, 8))
        o[:, 0] = np.bincount(ii, weights=has_dtc[m], minlength=n)
        o[:, 1] = np.bincount(ii, weights=is_hb[m], minlength=n)
        o[:, 2] = np.bincount(ii, weights=is_ha[m], minlength=n)
        o[:, 3] = np.bincount(ii, weights=(state[m] == 2), minlength=n) / cnt
        omax = np.full(n, -np.inf)
        omin = np.full(n, np.inf)
        np.maximum.at(omax, ii, odo[m])
        np.minimum.at(omin, ii, odo[m])
        o[:, 4] = np.where(np.isfinite(omax) & np.isfinite(omin), np.maximum(omax - omin, 0.0), 0.0)
        spd = np.bincount(ii, weights=(speed[m] * moving[m]), minlength=n)
        mcnt = np.maximum(np.bincount(ii, weights=moving[m], minlength=n), 1.0)
        o[:, 5] = spd / mcnt
        o[:, 6] = np.bincount(ii, weights=night[m], minlength=n) / cnt
        o[:, 7] = np.bincount(ii, weights=is_over[m], minlength=n)
        mat[:, b * 8:(b + 1) * 8] = o
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    mat = np.where(norms > 0, mat / np.maximum(norms, 1e-12), 0.0)
    return {"vin": vins, "matrix": mat}


def knn_search(query: np.ndarray, corpus: np.ndarray, k: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """Brute-force cosine kNN reference. Returns (indices, similarities), top-k first.

    Zero vectors have 0 similarity to everything (no NaNs).
    """
    q = np.asarray(query, dtype=float).reshape(-1)
    c = np.asarray(corpus, dtype=float)
    if c.shape[0] == 0 or q.shape[0] != DIM or c.shape[1] != DIM:
        return np.zeros(0, dtype=np.int64), np.zeros(0)
    qn = np.linalg.norm(q)
    cn = np.linalg.norm(c, axis=1)
    sims = (c @ q) / np.maximum(cn * max(qn, 1e-12), 1e-12)
    sims = np.where((cn > 0) & (qn > 0), sims, 0.0)
    k = min(max(k, 0), c.shape[0])
    order = np.argsort(sims, kind="stable")[::-1][:k]
    return order.astype(np.int64), sims[order]
