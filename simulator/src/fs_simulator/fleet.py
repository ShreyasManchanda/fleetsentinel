"""Deterministic fleet model: tenants, fleets, vehicles (valid ISO-3779 VINs), drivers. One integer seed drives everything.

Struct-of-arrays layout (numpy) so the behaviour engine can slice by vehicle without Python loops.
"""
from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from fscore.contracts import NAMESPACE_FS, tenant_id
from fscore.hashing import vin_shard
from fscore.vin import make_vin

from .constants import (
    MODELS,
    OEM_CODES,
    OEM_WMIS,
    OEMS_FOR_POWERTRAIN,
    PT_EV,
    PT_HYBRID,
    PT_ICE,
    demo_tenants_path,
)

DRIVERS_PER_VEHICLE = 1.2
GENERATED_MIN_SIZE = 20
SW_VERSIONS = {
    "OEM_A": ("4.2.1", "4.2.0", "4.1.7"),
    "OEM_B": ("2.9.0", "2.8.3"),
    "OEM_C": ("1.0.4", "1.0.3"),
    "OEM_D": ("5.2", "5.1"),
}
REGION_FOR_TENANT = ("IN-KA", "US-CA")


def _u5(kind: str, key: str) -> str:
    return str(uuid.uuid5(NAMESPACE_FS, f"{kind}|{key}"))


@dataclass(slots=True)
class TenantSpec:
    slug: str
    name: str
    tenant_id: str
    size: int
    ev_share: float
    hybrid_share: float
    lender: bool = False
    watchlist: int = 0
    named: bool = False
    region: str = "IN-KA"


@dataclass(slots=True)
class FleetSpec:
    fleet_id: str
    tenant_idx: int
    name: str
    region: str
    sites: tuple[int, int]
    n_vehicles: int


@dataclass
class Fleet:
    seed: int
    tenants: list[TenantSpec]
    fleets: list[FleetSpec]
    vin: np.ndarray                 # (N,) U17
    tenant_idx: np.ndarray          # (N,) int16
    fleet_idx: np.ndarray           # (N,) int32
    oem_idx: np.ndarray             # (N,) int8
    model_idx: np.ndarray           # (N,) int8 index into MODELS
    powertrain: np.ndarray          # (N,) int8
    home_site: np.ndarray           # (N,) int16
    driver_idx: np.ndarray          # (N,) int32 primary driver
    sw_idx: np.ndarray              # (N,) int8
    watchlist: np.ndarray           # (N,) bool
    shard: np.ndarray               # (N,) int16 murmur2 shard (64)
    driver_fleet: np.ndarray        # (D,) int32
    driver_tenant: np.ndarray       # (D,) int16
    driver_aggr: np.ndarray         # (D,) float, aggressiveness 0..1
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return int(self.vin.shape[0])

    @property
    def n_drivers(self) -> int:
        return int(self.driver_fleet.shape[0])

    def sw_versions(self) -> np.ndarray:
        out = np.empty(self.n, dtype=object)
        for o, code in enumerate(OEM_CODES):
            m = self.oem_idx == o
            versions = np.array(SW_VERSIONS[code], dtype=object)
            out[m] = versions[np.minimum(self.sw_idx[m], len(versions) - 1)]
        return out

    @staticmethod
    def driver_ref(driver_indices: np.ndarray) -> list[str]:
        """Opaque driver reference sent in telemetry (drv_<hex>); the normalizer encrypts it per subject."""
        return [f"drv_{int(i):06x}" for i in driver_indices]

    def vehicle_id(self, i: int) -> str:
        return _u5("vehicle", str(self.vin[i]))

    def summary(self) -> dict[str, Any]:
        pt = np.bincount(self.powertrain, minlength=3) / max(self.n, 1)
        oem = np.bincount(self.oem_idx, minlength=4)
        return {
            "vehicles": self.n,
            "tenants": len(self.tenants),
            "fleets": len(self.fleets),
            "drivers": self.n_drivers,
            "drivers_per_vehicle": round(self.n_drivers / max(self.n, 1), 3),
            "powertrain_share": {"ICE": round(float(pt[0]), 4), "EV": round(float(pt[1]), 4), "HYBRID": round(float(pt[2]), 4)},
            "oem_counts": {c: int(oem[i]) for i, c in enumerate(OEM_CODES)},
            "largest_tenant": max(t.size for t in self.tenants),
            "watchlist": int(self.watchlist.sum()),
        }


def load_tenant_config(path: Path | None = None) -> dict[str, Any]:
    with open(path or demo_tenants_path(), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def power_law_sizes(rng: np.random.Generator, total: int, k: int, alpha: float = 1.0, min_size: int = GENERATED_MIN_SIZE) -> np.ndarray:
    """k sizes summing exactly to total, Zipf-like (rank^-alpha) with a floor of min_size, shuffled."""
    if k <= 0:
        return np.zeros(0, dtype=np.int64)
    min_size = min(min_size, total // k)
    raw = np.arange(1, k + 1, dtype=float) ** -alpha
    spare = total - min_size * k
    ideal = raw / raw.sum() * spare
    sizes = np.floor(ideal).astype(np.int64)
    short = spare - int(sizes.sum())
    if short > 0:                                    # largest-remainder rounding so the sum is exact
        order = np.argsort(-(ideal - sizes))
        sizes[order[:short]] += 1
    sizes += min_size
    rng.shuffle(sizes)
    return sizes


def build_tenants(seed: int, cfg: dict[str, Any], total: int) -> list[TenantSpec]:
    rng = np.random.default_rng([seed, 1])
    cfg_total = int(cfg["total_vehicles"])
    scale = total / cfg_total
    tenants: list[TenantSpec] = []
    for t in cfg["tenants"]:
        size = int(t["vehicles"]) if total == cfg_total else max(1, round(t["vehicles"] * scale))
        wl = int(t.get("watchlist", 0) or 0)
        tenants.append(TenantSpec(
            slug=t["slug"], name=t["name"], tenant_id=tenant_id(t["slug"]), size=size,
            ev_share=float(t["ev_share"]), hybrid_share=float(t["hybrid_share"]),
            lender=bool(t.get("lender", False)), watchlist=wl if total == cfg_total else round(wl * scale), named=True,
        ))
    n_gen = int(cfg["generated_tenants"])
    remaining = total - sum(t.size for t in tenants)
    if remaining < n_gen:
        raise ValueError(f"total_vehicles={total} too small for {len(tenants)} named + {n_gen} generated tenants")
    sizes = power_law_sizes(rng, remaining, n_gen)
    named_ev = sum(t.ev_share * t.size for t in tenants)
    named_hy = sum(t.hybrid_share * t.size for t in tenants)
    gen_total = max(int(sizes.sum()), 1)
    # aim for a 60/30/10 ICE/EV/hybrid mix overall (spec 6.1) by steering the generated tenants
    gen_ev = float(np.clip((0.30 * total - named_ev) / gen_total, 0.05, 0.6))
    gen_hy = float(np.clip((0.10 * total - named_hy) / gen_total, 0.02, 0.3))
    for i, size in enumerate(sizes):
        ev = float(np.clip(rng.normal(gen_ev, 0.08), 0.02, 0.8))
        hy = float(np.clip(rng.normal(gen_hy, 0.04), 0.0, 0.3))
        if ev + hy > 0.95:
            hy = 0.95 - ev
        slug = f"fleet-{i + 1:02d}"
        tenants.append(TenantSpec(slug=slug, name=f"Demo Fleet {i + 1:02d}", tenant_id=tenant_id(slug),
                                  size=int(size), ev_share=ev, hybrid_share=hy))
    for i, t in enumerate(tenants):
        t.region = REGION_FOR_TENANT[1] if (i % 9 == 8) else REGION_FOR_TENANT[0]
    return tenants


def _pick_model(rng: np.random.Generator, oem: int, pt: int, n: int) -> np.ndarray:
    cands = [i for i, m in enumerate(MODELS) if m[0] == oem and m[2] == pt]
    if len(cands) == 1:
        return np.full(n, cands[0], dtype=np.int8)
    return rng.choice(cands, size=n, p=[0.6, 0.4]).astype(np.int8)


def _assign_powertrain_oem(rng: np.random.Generator, tenants: list[TenantSpec], n: int):
    powertrain = np.zeros(n, dtype=np.int8)
    oem_idx = np.zeros(n, dtype=np.int8)
    model_idx = np.zeros(n, dtype=np.int8)
    start = 0
    for t in tenants:
        sl = slice(start, start + t.size)
        n_ev = round(t.ev_share * t.size)
        n_hy = round(t.hybrid_share * t.size)
        pts = np.array([PT_EV] * n_ev + [PT_HYBRID] * n_hy + [PT_ICE] * (t.size - n_ev - n_hy), dtype=np.int8)
        rng.shuffle(pts)
        powertrain[sl] = pts
        affinity = rng.dirichlet(np.full(4, 2.0))
        oems = np.zeros(t.size, dtype=np.int8)
        models = np.zeros(t.size, dtype=np.int8)
        for pt in (PT_ICE, PT_EV, PT_HYBRID):
            m = pts == pt
            cnt = int(m.sum())
            if not cnt:
                continue
            allowed = np.array(OEMS_FOR_POWERTRAIN[pt])
            chosen = rng.choice(allowed, size=cnt, p=affinity[allowed] / affinity[allowed].sum())
            oems[m] = chosen
            sub_models = np.zeros(cnt, dtype=np.int8)
            for o in allowed:
                mm = chosen == o
                if mm.any():
                    sub_models[mm] = _pick_model(rng, int(o), pt, int(mm.sum()))
            models[m] = sub_models
        oem_idx[sl] = oems
        model_idx[sl] = models
        start += t.size
    return powertrain, oem_idx, model_idx


def _assign_fleets(rng: np.random.Generator, tenants: list[TenantSpec], n: int, n_sites: int):
    fleets: list[FleetSpec] = []
    fleet_idx = np.zeros(n, dtype=np.int32)
    home_site = np.zeros(n, dtype=np.int16)
    start = 0
    for ti, t in enumerate(tenants):
        n_fleets = int(np.clip(round(t.size / 2500), 1, 12))
        weights = rng.dirichlet(np.full(n_fleets, 3.0))
        assign = rng.choice(n_fleets, size=t.size, p=weights)
        first = len(fleets)
        for k in range(n_fleets):
            s0, s1 = (int(x) for x in rng.choice(n_sites, size=2, replace=False))
            fleets.append(FleetSpec(_u5("fleet", f"{t.slug}|{k}"), ti, f"{t.name} Fleet {k + 1}", t.region,
                                    (s0, s1), int((assign == k).sum())))
        fleet_idx[start:start + t.size] = first + assign
        sites_arr = np.array([f.sites for f in fleets[first:]], dtype=np.int16)      # (n_fleets, 2)
        home_site[start:start + t.size] = sites_arr[assign, rng.integers(0, 2, t.size)]
        start += t.size
    return fleets, fleet_idx, home_site


def _make_vins(pyrng: random.Random, oem_idx: np.ndarray) -> np.ndarray:
    n = len(oem_idx)
    vins = np.empty(n, dtype="<U17")
    seen: set[str] = set()
    for i in range(n):
        wmis = OEM_WMIS[OEM_CODES[oem_idx[i]]]
        wmi = wmis[0] if pyrng.random() < 0.5 else wmis[1]
        v = make_vin(wmi, pyrng)
        while v in seen:
            v = make_vin(wmi, pyrng)
        seen.add(v)
        vins[i] = v
    return vins


def _assign_drivers(rng: np.random.Generator, fleets: list[FleetSpec], fleet_idx: np.ndarray):
    """About 1.2 drivers per vehicle, per fleet; the primary driver of each vehicle is unique within its fleet."""
    driver_idx = np.zeros(len(fleet_idx), dtype=np.int32)
    d_fleet: list[np.ndarray] = []
    d_ten: list[np.ndarray] = []
    offset = 0
    order = np.argsort(fleet_idx, kind="stable")
    counts = np.bincount(fleet_idx, minlength=len(fleets))
    pos = 0
    for fi, cnt in enumerate(counts):
        if cnt == 0:
            continue
        members = order[pos:pos + cnt]
        pos += cnt
        n_dr = max(1, math.ceil(DRIVERS_PER_VEHICLE * cnt))
        driver_idx[members] = offset + (np.arange(cnt) % n_dr)
        d_fleet.append(np.full(n_dr, fi, dtype=np.int32))
        d_ten.append(np.full(n_dr, fleets[fi].tenant_idx, dtype=np.int16))
        offset += n_dr
    return driver_idx, np.concatenate(d_fleet), np.concatenate(d_ten), rng.beta(2.0, 7.0, size=offset)


def generate_fleet(seed: int = 42, total_vehicles: int | None = None, tenants_path: Path | None = None, n_sites: int = 60) -> Fleet:
    """Generate the whole fleet deterministically from seed."""
    cfg = load_tenant_config(tenants_path)
    total = int(total_vehicles or cfg["total_vehicles"])
    tenants = build_tenants(seed, cfg, total)
    rng = np.random.default_rng([seed, 2])
    n = sum(t.size for t in tenants)
    assert n == total, (n, total)
    tenant_idx = np.repeat(np.arange(len(tenants), dtype=np.int16), [t.size for t in tenants])
    powertrain, oem_idx, model_idx = _assign_powertrain_oem(rng, tenants, n)
    fleets, fleet_idx, home_site = _assign_fleets(rng, tenants, n, n_sites)
    vins = _make_vins(random.Random(seed * 1_000_003 + 17), oem_idx)
    driver_idx, driver_fleet, driver_tenant, driver_aggr = _assign_drivers(rng, fleets, fleet_idx)
    sw_idx = rng.integers(0, 3, size=n).astype(np.int8)
    for o, code in enumerate(OEM_CODES):
        m = oem_idx == o
        sw_idx[m] = np.minimum(sw_idx[m], len(SW_VERSIONS[code]) - 1)
    watchlist = np.zeros(n, dtype=bool)
    for ti, t in enumerate(tenants):
        if t.watchlist:
            members = np.flatnonzero(tenant_idx == ti)
            watchlist[rng.choice(members, size=min(t.watchlist, len(members)), replace=False)] = True
    shard = np.fromiter((vin_shard(str(v)) for v in vins), dtype=np.int16, count=n)
    return Fleet(seed, tenants, fleets, vins, tenant_idx, fleet_idx, oem_idx, model_idx, powertrain, home_site,
                 driver_idx, sw_idx, watchlist, shard, driver_fleet, driver_tenant, driver_aggr)
