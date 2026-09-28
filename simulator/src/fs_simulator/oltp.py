"""OLTP seed tables (tenant, subscription, fleet, depot, subject_key, driver, vehicle, driver_assignment, trip) as Arrow tables
shaped for COPY into db/migrations/0001_core.sql."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import numpy as np
import pyarrow as pa
from faker import Faker
from fscore.contracts import NAMESPACE_FS, trip_id

from .constants import MODELS, PT_EV, TARIFFS
from .crypto import KekWrapper, encrypt_pii, new_deks
from .fleet import Fleet
from .routes import RouteLibrary

TABLE_ORDER = ["tenant", "subscription", "fleet", "depot", "subject_key", "driver", "vehicle", "driver_assignment", "trip"]


def _u5(kind: str, key: str) -> str:
    return str(uuid.uuid5(NAMESPACE_FS, f"{kind}|{key}"))


@dataclass
class RefIds:
    """Reference ids (tariff_region / vehicle_model). Filled from the database when available, deterministic otherwise."""
    tariff: dict[str, str] = field(default_factory=lambda: {c: _u5("ref-tariff", c) for c in TARIFFS})
    model: list[str] = field(default_factory=lambda: [_u5("ref-model", m[1]) for m in MODELS])


def _hex(b: bytes) -> str:
    return chr(92) + "x" + b.hex()


def driver_ids(fleet: Fleet) -> list[str]:
    return [_u5("driver", str(i)) for i in range(fleet.n_drivers)]


def vehicle_ids(fleet: Fleet) -> list[str]:
    return [_u5("vehicle", v) for v in fleet.vin.tolist()]


def structure_tables(fleet: Fleet, routes: RouteLibrary, refs: RefIds) -> dict[str, pa.Table]:
    ten, fl = fleet.tenants, fleet.fleets
    out: dict[str, pa.Table] = {}
    out["tenant"] = pa.table({"id": [t.tenant_id for t in ten], "name": [t.name for t in ten],
                              "data_region": ["ap-south-1"] * len(ten), "currency": ["USD"] * len(ten)})
    plan = ["enterprise" if t.size > 5000 else "growth" if t.size > 500 else "starter" for t in ten]
    out["subscription"] = pa.table({"id": [_u5("subscription", t.slug) for t in ten], "tenant_id": [t.tenant_id for t in ten],
                                    "plan": plan, "seats": [max(5, t.size // 200) for t in ten], "status": ["active"] * len(ten)})
    out["fleet"] = pa.table({"id": [f.fleet_id for f in fl], "tenant_id": [ten[f.tenant_idx].tenant_id for f in fl],
                             "tariff_region_id": [refs.tariff[f.region] for f in fl], "name": [f.name for f in fl]})
    d: dict[str, list] = {k: [] for k in ("id", "tenant_id", "fleet_id", "name", "lat", "lon", "radius_m")}
    for f in fl:
        for k, s in enumerate(f.sites):
            d["id"].append(_u5("depot", f"{f.fleet_id}|{s}"))
            d["tenant_id"].append(ten[f.tenant_idx].tenant_id)
            d["fleet_id"].append(f.fleet_id)
            d["name"].append(f"{f.name} Depot {k + 1}")
            d["lat"].append(float(routes.site_pos[s, 0]))
            d["lon"].append(float(routes.site_pos[s, 1]))
            d["radius_m"].append(300)
    out["depot"] = pa.table(d)
    return out


def people_tables(fleet: Fleet, wrapper: KekWrapper, seed: int, refs: RefIds, anchor_iso: str,
                  deterministic_deks: bool = False) -> tuple[dict[str, pa.Table], dict[str, bytes]]:
    """Drivers + subject keys (AES-256-GCM PII, DEKs wrapped in batches), vehicles, assignments."""
    ten, fl = fleet.tenants, fleet.fleets
    rng = np.random.default_rng([seed, 5])
    n_d = fleet.n_drivers
    faker = Faker("en_IN")
    faker.seed_instance(seed)
    sk_ids = [f"sk-{_u5('subject', str(i)).replace('-', '')[:20]}" for i in range(n_d)]
    deks = new_deks(n_d, np.random.default_rng([seed, 6]) if deterministic_deks else None)
    wrapped = wrapper.wrap_many(deks)
    lic = rng.integers(0, 10_000_000, n_d).tolist()
    name_enc = [_hex(encrypt_pii(deks[i], sk_ids[i], faker.name())) for i in range(n_d)]
    lic_enc = [_hex(encrypt_pii(deks[i], sk_ids[i], f"KA{(i % 50) + 1:02d}{2005 + i % 15}{lic[i]:07d}")) for i in range(n_d)]
    d_tenant = [ten[t].tenant_id for t in fleet.driver_tenant.tolist()]
    out: dict[str, pa.Table] = {}
    out["subject_key"] = pa.table({"subject_key_id": sk_ids, "tenant_id": d_tenant, "wrapped_dek": [_hex(w) for w in wrapped]})
    did = driver_ids(fleet)
    out["driver"] = pa.table({"id": did, "tenant_id": d_tenant, "fleet_id": [fl[i].fleet_id for i in fleet.driver_fleet.tolist()],
                              "name_enc": name_enc, "license_enc": lic_enc, "subject_key_id": sk_ids})
    vid = vehicle_ids(fleet)
    v_tenant = [ten[t].tenant_id for t in fleet.tenant_idx.tolist()]
    out["vehicle"] = pa.table({"id": vid, "tenant_id": v_tenant, "fleet_id": [fl[i].fleet_id for i in fleet.fleet_idx.tolist()],
                               "model_id": [refs.model[i] for i in fleet.model_idx.tolist()], "vin": fleet.vin.tolist()})
    out["driver_assignment"] = pa.table({
        "id": [_u5("assignment", v) for v in vid], "tenant_id": v_tenant, "vehicle_id": vid,
        "driver_id": [did[i] for i in fleet.driver_idx.tolist()], "valid_during": [f"[{anchor_iso},)"] * fleet.n})
    return out, dict(zip(sk_ids, deks, strict=True))


def build_tables(fleet: Fleet, routes: RouteLibrary, wrapper: KekWrapper, seed: int, refs: RefIds | None = None,
                 anchor_iso: str = "2026-08-29 00:00:00+00", deterministic_deks: bool = False):
    refs = refs or RefIds()
    tables = structure_tables(fleet, routes, refs)
    people, deks = people_tables(fleet, wrapper, seed, refs, anchor_iso, deterministic_deks)
    tables.update(people)
    return tables, deks


def _price_table() -> np.ndarray:
    """(region, energy, hour) -> USD per unit; energies: 0 DIESEL, 1 PETROL, 2 ELECTRICITY."""
    out = np.zeros((len(TARIFFS), 3, 24))
    for ri, region in enumerate(TARIFFS):
        for ei, energy in enumerate(("DIESEL", "PETROL", "ELECTRICITY")):
            for lo, hi, price in TARIFFS[region][energy]:
                out[ri, ei, lo:hi] = price
    return out


def trips_to_table(fleet: Fleet, trips: list[dict[str, np.ndarray]], vid: list[str], did: list[str]) -> pa.Table:
    """Concatenate engine trip aggregates into the trip table (deterministic ids via fscore.contracts.trip_id)."""
    cat = {k: np.concatenate([t[k] for t in trips]) for k in trips[0]}
    g = cat["gidx"]
    start_ms = (cat["start_s"] * 1000).astype(np.int64)
    end_ms = (cat["end_s"] * 1000).astype(np.int64)
    fleet_region = np.array([list(TARIFFS).index(f.region) for f in fleet.fleets])
    region = fleet_region[fleet.fleet_idx[g]]
    hour = (((cat["start_s"] + 19800) % 86400) // 3600).astype(int)
    energy_kind = np.where(fleet.powertrain[g] == PT_EV, 2, np.where(fleet.model_idx[g] == 2, 0, 1))
    cost = cat["energy"] * _price_table()[region, energy_kind, hour]
    vins = fleet.vin[g].tolist()
    tid = [fleet.tenants[i].tenant_id for i in fleet.tenant_idx[g].tolist()]
    return pa.table({
        "id": [trip_id(v, s) for v, s in zip(vins, start_ms.tolist(), strict=True)], "tenant_id": tid,
        "vehicle_id": [vid[i] for i in g.tolist()], "driver_id": [did[i] for i in fleet.driver_idx[g].tolist()],
        "start_ts": pa.array(start_ms, pa.timestamp("ms", tz="UTC")), "end_ts": pa.array(end_ms, pa.timestamp("ms", tz="UTC")),
        "distance_km": np.round(cat["dist_km"], 2), "idle_s": cat["idle_s"].astype(np.int32),
        "energy_used": np.round(cat["energy"], 2), "energy_cost": np.round(cost, 2),
        "harsh_brake_count": cat["hb"].astype(np.int32), "harsh_accel_count": cat["ha"].astype(np.int32),
        "overspeed_s": cat["over_s"].astype(np.int32), "night_share": np.round(np.clip(cat["night_share"], 0, 1), 3)})
