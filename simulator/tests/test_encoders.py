from __future__ import annotations

import json

import numpy as np
import pytest
from fs_simulator.constants import OEM_CODES, mappings_dir
from fs_simulator.encoders import D_COLS, EncodeContext, encode, encode_d_rows, iso_ms, wrap_batch
from fscore.mapping_engine import Mapping
from helpers_sim import make_sim, run

MAP_FILE = {"OEM_A": "oem_a", "OEM_B": "oem_b", "OEM_C": "oem_c", "OEM_D": "oem_d_draft"}


@pytest.fixture(scope="module")
def emissions(fleet, routes):
    sim, _ = make_sim(fleet, routes, seed=4, t0=1_790_000_000.0 + 3 * 3600)
    return [e for e in run(sim, 200)[60:200:5] if len(e)]


@pytest.fixture(scope="module")
def ctx(fleet):
    return EncodeContext.from_fleet(fleet)


@pytest.mark.parametrize("oem", OEM_CODES)
def test_roundtrip_through_mapping_engine_zero_failures(oem, emissions, ctx, fleet):
    mp = Mapping.from_yaml((mappings_dir() / f"{MAP_FILE[oem]}.yaml").read_text(encoding="utf-8"))
    oi = OEM_CODES.index(oem)
    total = 0
    for em in emissions:
        sub = em.select(fleet.oem_idx[em.gidx] == oi)
        if not len(sub):
            continue
        shape, recs = encode(oem, sub, ctx)
        res = mp.apply(wrap_batch(shape, recs, 1, int(sub.ts_ms[0])), now_ms=int(sub.ts_ms.max()) + 10**9)
        assert res.failed == [] and len(res.ok) == len(sub)
        for got, vin, ts, lat in zip(res.ok, fleet.vin[sub.gidx], sub.ts_ms, sub.lat, strict=True):
            assert got["vin"] == vin and got["oem"] == oem and abs(got["lat"] - lat) < 1e-5
            assert abs(got["ts_ms"] - ts) < (1000 if oem == "OEM_D" else 1)
        total += len(sub)
    assert total > 50


def test_units_roundtrip_b_and_d(emissions, ctx, fleet):
    em = next(e for e in emissions if (fleet.oem_idx[e.gidx] == 1).any() and (e.speed > 1).any())
    sub = em.select((fleet.oem_idx[em.gidx] == 1) & (em.speed > 1))
    shape, recs = encode("OEM_B", sub, ctx)
    got = Mapping.from_yaml((mappings_dir() / "oem_b.yaml").read_text()).apply(wrap_batch(shape, recs, 2, 1), now_ms=10**13).ok
    assert all(abs(g["speed_kmh"] - s) < 0.12 for g, s in zip(got, sub.speed, strict=True))
    assert all(abs(g["odo_km"] - o) < 0.15 for g, o in zip(got, sub.odo, strict=True))
    assert all(0 <= g["fuel_pct"] <= 100 for g in got)


@pytest.mark.parametrize("oem", OEM_CODES)
def test_batch_shape_matches_sample(oem, emissions, ctx, fleet):
    sample = json.loads((mappings_dir() / "samples" / f"{oem.lower()}.json").read_text(encoding="utf-8"))
    em = next(e for e in emissions if (fleet.oem_idx[e.gidx] == OEM_CODES.index(oem)).any())
    sub = em.select(fleet.oem_idx[em.gidx] == OEM_CODES.index(oem))
    shape, recs = encode(oem, sub, ctx)
    batch = json.loads(wrap_batch(shape, recs, 7, int(sub.ts_ms[0])))
    assert set(batch) == set(sample)                     # same envelope keys
    key = {"OEM_A": "records", "OEM_B": "d", "OEM_C": "events", "OEM_D": "rows"}[oem]
    if oem == "OEM_D":
        assert batch["cols"] == sample["cols"] == D_COLS and batch["schema"] == sample["schema"]
        assert all(len(r) == len(D_COLS) for r in batch[key])
    else:
        allowed = set(sample[key][0]) | {"ignition", "battery", "fl"}
        assert all(set(r) <= allowed for r in batch[key])
    assert batch[list(batch)[0]].startswith(oem[-1].lower() + "-")


def test_iso_ms_and_legacy_d(emissions, ctx, fleet):
    assert iso_ms(np.array([1790590502120]))[0] == "2026-09-28T10:15:02.120Z"
    em = emissions[0]
    shape, recs = encode("OEM_D", em, ctx, d_legacy=True)
    assert shape == "OEM_C" and isinstance(recs[0], dict)
    assert len(encode_d_rows(em, ctx)) == len(em)
