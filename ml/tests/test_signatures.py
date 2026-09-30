"""Tests for the 32-dim signature builder + brute-force kNN reference."""
from __future__ import annotations

import numpy as np
import pyarrow as pa
import pytest
from fs_ml.signatures import DIM, build_signatures, knn_search
from hypothesis import given
from hypothesis import strategies as st

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


def test_shape_and_l2_normalized():
    t = _day_table(["V1", "V1", "V2"], [1, 7, 13], ["HARSH_BRAKE", "POSITION", "POSITION"])
    out = build_signatures(t, DAY)
    assert out["matrix"].shape == (2, DIM)
    assert out["vin"].tolist() == ["V1", "V2"]
    norms = np.linalg.norm(out["matrix"], axis=1)
    assert norms.tolist() == pytest.approx([1.0, 1.0])


def test_buckets_separate_time():
    morning = _day_table(["V1"], [2], ["HARSH_BRAKE"])
    evening = _day_table(["V1"], [20], ["HARSH_BRAKE"])
    m = build_signatures(morning, DAY)["matrix"][0]
    e = build_signatures(evening, DAY)["matrix"][0]
    assert not np.allclose(m, e)
    assert m[:8].sum() > 0 and m[8:].sum() == 0
    assert e[:24].sum() == 0 and e[24:].sum() > 0


def test_empty_and_out_of_window():
    t = _day_table(["V1"], [30], ["HARSH_BRAKE"])  # next day
    out = build_signatures(t, DAY)
    assert out["matrix"].shape == (0, DIM)


def test_knn_self_match_first():
    rng = np.random.default_rng(0)
    corpus = rng.normal(size=(20, DIM))
    corpus /= np.linalg.norm(corpus, axis=1, keepdims=True)
    idx, sims = knn_search(corpus[7], corpus, k=5)
    assert idx[0] == 7 and sims[0] == 1.0
    assert len(idx) == 5 and ((sims >= -1.0) & (sims <= 1.0)).all()


def test_knn_zero_vectors_safe():
    idx, sims = knn_search(np.zeros(DIM), np.zeros((4, DIM)), k=2)
    assert (sims == 0.0).all()
    assert knn_search(np.zeros(3), np.zeros((4, 3)), k=2)[0].size == 0


@given(n=st.integers(1, 10), k=st.integers(0, 12))
def test_knn_returns_topk_sorted(n, k):
    rng = np.random.default_rng(n * 100 + k)
    corpus = rng.normal(size=(n, DIM))
    q = rng.normal(size=DIM)
    idx, sims = knn_search(q, corpus, k=k)
    assert len(idx) == min(max(k, 0), n)
    assert (np.diff(sims) <= 1e-12).all()  # descending
