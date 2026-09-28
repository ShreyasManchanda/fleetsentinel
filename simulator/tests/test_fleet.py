from __future__ import annotations

import re

import numpy as np
import pytest
from fs_simulator.constants import MODELS, OEM_CODES, OEMS_FOR_POWERTRAIN, repo_root
from fs_simulator.fleet import generate_fleet, load_tenant_config, power_law_sizes
from fscore.contracts import tenant_id
from fscore.vin import is_valid_vin
from hypothesis import given, settings
from hypothesis import strategies as st


def test_full_fleet_shape(full_fleet):
    f = full_fleet
    cfg = load_tenant_config()
    assert f.n == 100_000 == cfg["total_vehicles"]
    assert len(f.tenants) == 50
    named = {t["slug"]: t["vehicles"] for t in cfg["tenants"]}
    for t in f.tenants[:5]:
        assert t.size == named[t.slug]
        assert t.tenant_id == tenant_id(t.slug)
    assert sum(t.size for t in f.tenants) == 100_000
    assert max(t.size for t in f.tenants) == 30_000
    assert min(t.size for t in f.tenants) >= 20


def test_vins_valid_unique_and_wmi_per_oem(full_fleet):
    f = full_fleet
    assert len(set(f.vin.tolist())) == f.n
    assert all(is_valid_vin(v) for v in f.vin.tolist())
    from fs_simulator.constants import OEM_WMIS
    for o, code in enumerate(OEM_CODES):
        wmis = {v[:3] for v in f.vin[f.oem_idx == o].tolist()}
        assert wmis <= set(OEM_WMIS[code]) and wmis


def test_powertrain_mix_and_oem_constraints(full_fleet):
    f = full_fleet
    share = np.bincount(f.powertrain, minlength=3) / f.n
    assert 0.55 < share[0] < 0.68 and 0.24 < share[1] < 0.36 and 0.05 < share[2] < 0.14
    for pt, oems in OEMS_FOR_POWERTRAIN.items():
        assert set(np.unique(f.oem_idx[f.powertrain == pt]).tolist()) <= set(oems)
    for mi, m in enumerate(MODELS):
        sel = f.model_idx == mi
        assert (f.oem_idx[sel] == m[0]).all() and (f.powertrain[sel] == m[2]).all()
    metro = next(i for i, t in enumerate(f.tenants) if t.slug == "metro-cabs-ev")
    assert (f.powertrain[f.tenant_idx == metro] == 1).mean() > 0.89


def test_drivers_watchlist_and_shards(full_fleet):
    f = full_fleet
    assert abs(f.n_drivers / f.n - 1.2) < 0.02
    assert f.watchlist.sum() == 500
    kest = next(i for i, t in enumerate(f.tenants) if t.slug == "kestrel-finance")
    assert f.watchlist[f.tenant_idx == kest].sum() == 500
    assert f.shard.min() >= 0 and f.shard.max() < 64
    assert (f.driver_tenant[f.driver_idx] == f.tenant_idx).all()


def test_deterministic_and_seed_sensitive():
    a, b, c = generate_fleet(5, 1500), generate_fleet(5, 1500), generate_fleet(6, 1500)
    assert a.vin.tolist() == b.vin.tolist() and (a.oem_idx == b.oem_idx).all()
    assert a.vin.tolist() != c.vin.tolist()


def test_vehicle_models_match_reference_sql():
    sql = (repo_root() / "db" / "migrations" / "0005_reference.sql").read_text(encoding="utf-8")
    rows = re.findall(r"\('(OEM_[A-D])',\s*'([^']+)',\s*'(ICE|EV|HYBRID)',\s*([\w.:]+),\s*([\w.:]+),\s*([\w.:]+)\)", sql)
    assert len(rows) == len(MODELS)
    ref = {(r[0], r[1], r[2]) for r in rows}
    mine = {(OEM_CODES[m[0]], m[1], ("ICE", "EV", "HYBRID")[m[2]]) for m in MODELS}
    assert ref == mine
    for r in rows:
        m = next(x for x in MODELS if x[1] == r[1])
        for got, want in ((r[3], m[3]), (r[4], m[4]), (r[5], m[5])):
            assert (None if got.startswith("NULL") else float(got.split("::")[0])) == want


def test_total_too_small_raises():
    with pytest.raises(ValueError):
        generate_fleet(1, 60)


@settings(max_examples=60, deadline=None)
@given(total=st.integers(0, 200_000), k=st.integers(1, 60), seed=st.integers(0, 2**31))
def test_power_law_sizes_exact_sum(total, k, seed):
    s = power_law_sizes(np.random.default_rng(seed), total, k)
    assert int(s.sum()) == total and len(s) == k and (s >= 0).all()
    if total >= 20 * k:
        assert s.min() >= 20
