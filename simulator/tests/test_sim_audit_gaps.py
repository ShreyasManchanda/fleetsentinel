"""Audit gap-fillers for the simulator: encoder round-trip precision per field,
event-code preservation per OEM, Mapping-vs-FastMapping on real emissions,
noise ledger conservation, fail-closed KEK edges, wear/label/feature corners.
"""
from __future__ import annotations

import json
import re

import numpy as np
import pyarrow as pa
import pytest
import yaml
from fs_simulator import groundtruth as G
from fs_simulator.behaviour import Emission, SimConfig
from fs_simulator.constants import (
    DTC_INDEX,
    EVT,
    EVT_NAMES,
    OEM_CODES,
    PT_EV,
    PT_ICE,
    demo_tenants_path,
    mappings_dir,
)
from fs_simulator.crypto import LocalDevKek, decrypt_pii, encrypt_pii, new_deks
from fs_simulator.encoders import EncodeContext, encode, encode_d_rows, iso_ms, wrap_batch
from fs_simulator.fleet import generate_fleet
from fs_simulator.groundtruth import GroundTruth, SeqLedger
from fs_simulator.noise import NoiseInjector
from fs_simulator.publisher import Connector, DriftControl, ListTransport, SimPublisher, is_event_lane
from fs_simulator.wear import WearModel, hazard_per_day
from fscore.hashing import vin_shard
from fscore.mapping_engine import Mapping
from fscore.mapping_fast import FastMapping
from helpers_sim import T0, make_sim
from hypothesis import given, settings
from hypothesis import strategies as st

T_MORNING = 1_790_000_000.0 - (1_790_000_000.0 % 86400) - 19800 + 6 * 3600


@pytest.fixture(scope="module")
def ctx(fleet):
    return EncodeContext.from_fleet(fleet)


@pytest.fixture(scope="module")
def emissions(fleet, routes):
    sim, _ = make_sim(fleet, routes, seed=4, t0=1_790_000_000.0 + 3 * 3600)
    return [e for e in (sim.step(60.0) for _ in range(120)) if len(e)]


def _mapping(oem: str) -> Mapping:
    name = "oem_d_draft" if oem == "OEM_D" else oem.lower()
    return Mapping.from_yaml((mappings_dir() / f"{name}.yaml").read_text(encoding="utf-8"))


def _one(em: Emission, code: str) -> Emission:
    one = em.select(np.array([0]))
    one.evt[0] = EVT[code]
    one.dtc[0] = -1
    return one


# ---------------------------------------------------------------- event codes
EXPECTED_EVT = {
    # Emission evt -> canonical evt after encode + mapping. Non-mappable OEM
    # codes collapse to POSITION by contract (never to a wrong event).
    "OEM_A": {c: c for c in EVT_NAMES},
    "OEM_B": {c: (c if c not in ("CHARGING_START", "CHARGING_END") else "POSITION") for c in EVT_NAMES},
    "OEM_C": {c: (c if c not in ("IGNITION_ON", "IGNITION_OFF", "CHARGING_START", "CHARGING_END") else "POSITION")
              for c in EVT_NAMES},
    "OEM_D": {c: (c if c not in ("IGNITION_ON", "IGNITION_OFF", "DTC_SET", "OVERSPEED") else "POSITION")
              for c in EVT_NAMES},
}


@pytest.mark.parametrize("oem", OEM_CODES)
def test_event_codes_roundtrip_exactly(oem, emissions, ctx, fleet):
    mp = _mapping(oem)
    oi = OEM_CODES.index(oem)
    em = next(e for e in emissions if (fleet.oem_idx[e.gidx] == oi).any())
    sub = em.select(fleet.oem_idx[em.gidx] == oi)
    for code in EVT_NAMES:
        one = _one(sub, code)
        shape, recs = encode(oem, one, ctx)
        res = mp.apply(wrap_batch(shape, recs, 1, int(one.ts_ms[0])), now_ms=int(one.ts_ms[0]) + 10**9)
        assert res.failed == [], (oem, code, res.failed)
        assert res.ok[0]["evt"] == EXPECTED_EVT[oem][code], (oem, code)


@pytest.mark.parametrize("oem", OEM_CODES)
def test_field_precision_and_passthrough(oem, emissions, ctx, fleet):
    mp = _mapping(oem)
    oi = OEM_CODES.index(oem)
    em = next(e for e in emissions if (fleet.oem_idx[e.gidx] == oi).any())
    sub = em.select((fleet.oem_idx[em.gidx] == oi) & (em.speed > 5))
    one = sub.select(np.array([0]))
    shape, recs = encode(oem, one, ctx)
    res = mp.apply(wrap_batch(shape, recs, 1, int(one.ts_ms[0])), now_ms=int(one.ts_ms[0]) + 10**9)
    assert res.failed == [], (oem, res.failed)
    got = res.ok[0]
    gi = int(one.gidx[0])
    assert got["vin"] == fleet.vin[gi] and got["oem"] == oem
    assert abs(got["lat"] - one.lat[0]) < 1e-5 and abs(got["lon"] - one.lon[0]) < 1e-5
    assert got["ignition"] == bool(one.ign[0])
    if oem in ("OEM_A", "OEM_C"):  # these shapes carry seq; B/D fall back to the engine default 0
        assert got["seq"] == int(one.seq[0])
    else:
        assert got["seq"] == 0
    if oem == "OEM_D":
        assert got["ts_ms"] == int(one.ts_ms[0] // 1000 * 1000)  # columnar epoch seconds floor
        assert got["sw_version"] == "nimbus/5.2"
    else:
        assert abs(got["ts_ms"] - int(one.ts_ms[0])) < 1
    if oem == "OEM_A":
        assert abs(got["speed_kmh"] - one.speed[0]) < 0.06 and abs(got["odo_km"] - one.odo[0]) < 0.06
        assert got["sw_version"] == ctx.sw[gi] and got["driver_ref"] == ctx.drv[gi]
    elif oem == "OEM_B":
        assert abs(got["speed_kmh"] - one.speed[0]) < 0.12 and abs(got["odo_km"] - one.odo[0]) < 0.15
        assert got["sw_version"] == ctx.sw[gi] and got["driver_ref"] == ctx.drv[gi]
    elif oem == "OEM_C":
        assert abs(got["speed_kmh"] - one.speed[0]) < 0.06
        assert got["seq"] == int(one.seq[0])

@pytest.mark.parametrize("oem", [o for o in OEM_CODES if o != "OEM_D"])  # OEM_D is EV-only
def test_ice_vehicles_omit_soc_and_still_map_clean(oem, emissions, ctx, fleet):
    mp = _mapping(oem)
    oi = OEM_CODES.index(oem)
    rows = 0
    for em in emissions:
        sub = em.select((fleet.oem_idx[em.gidx] == oi) & (fleet.powertrain[em.gidx] == PT_ICE))
        if not len(sub):
            continue
        assert bool(np.isnan(sub.soc).all())
        shape, recs = encode(oem, sub, ctx)
        res = mp.apply(wrap_batch(shape, recs, 1, int(sub.ts_ms[0])), now_ms=int(sub.ts_ms.max()) + 10**9)
        assert res.failed == [], (oem, res.failed)
        assert all("soc_pct" not in d for d in res.ok)  # optional field skipped, not nulled
        rows += len(sub)
        if rows > 30:
            break
    assert rows > 30


def test_soc_soh_precision_on_battery_vehicles(emissions, ctx, fleet):
    for oem in ("OEM_A", "OEM_C", "OEM_D"):
        mp = _mapping(oem)
        oi = OEM_CODES.index(oem)
        em = next(e for e in emissions
                  if ((fleet.oem_idx[e.gidx] == oi) & ~np.isnan(e.soc)).any())
        sub = em.select((fleet.oem_idx[em.gidx] == oi) & ~np.isnan(em.soc)).select(np.array([0]))
        shape, recs = encode(oem, sub, ctx)
        res = mp.apply(wrap_batch(shape, recs, 1, int(sub.ts_ms[0])), now_ms=int(sub.ts_ms[0]) + 10**9)
        assert res.failed == [], (oem, res.failed)
        assert abs(res.ok[0]["soc_pct"] - sub.soc[0]) < 0.06, oem
        if oem in ("OEM_A", "OEM_D"):
            assert abs(res.ok[0]["soh_pct"] - sub.soh[0]) < 0.06, oem


@pytest.mark.parametrize("oem", OEM_CODES)
def test_empty_emission_maps_to_empty_batch(oem, emissions, ctx):
    em = next(e for e in emissions if len(e))
    empty = em.select(np.zeros(len(em), dtype=bool))
    assert len(empty) == 0
    shape, recs = encode(oem, empty, ctx)
    assert recs == []
    res = _mapping(oem).apply(wrap_batch(shape, recs, 9, 1_790_590_000_000), now_ms=1_790_590_000_000 + 10**9)
    assert res.ok == [] and res.failed == []


def test_iso_ms_known_values():
    assert iso_ms(np.array([0]))[0] == "1970-01-01T00:00:00.000Z"
    assert iso_ms(np.array([1_790_590_502_120]))[0] == "2026-09-28T10:15:02.120Z"


@pytest.mark.parametrize("oem", OEM_CODES)
def test_batch_id_format(oem, emissions, ctx):
    em = next(e for e in emissions if len(e))
    shape, recs = encode(oem, em.select(np.array([0])), ctx)
    batch = json.loads(wrap_batch(shape, recs, 7, 1_790_590_000_000))
    bid = batch.get("batch") or batch.get("bid") or batch.get("batch_id")
    assert re.fullmatch(r"[a-d]-\d{6}", bid), bid
    assert len(encode_d_rows(em.select(np.array([0])), ctx)) == 1


# ---------------------------------------------------------------- fast path on real data
@pytest.mark.parametrize("oem", OEM_CODES)
def test_fast_mapping_matches_reference_on_real_emissions(oem, emissions, ctx, fleet):
    m = _mapping(oem)
    fm = FastMapping(m)
    oi = OEM_CODES.index(oem)
    checked = 0
    for em in emissions[5:45]:
        sub = em.select(fleet.oem_idx[em.gidx] == oi)
        if not len(sub):
            continue
        shape, recs = encode(oem, sub, ctx)
        raw = wrap_batch(shape, recs, 3, int(sub.ts_ms[0]))
        now = int(sub.ts_ms.max()) + 10**9
        ref, fast = m.apply(raw, now), fm.apply(raw, now)
        assert fast.ok == ref.ok and [(f.reason, f.detail) for f in fast.failed] == [
            (f.reason, f.detail) for f in ref.failed]
        assert not ref.failed
        checked += len(sub)
    assert checked > 50


def test_fast_mapping_matches_reference_on_legacy_d_shape(emissions, ctx, fleet):
    m = _mapping("OEM_D")
    fm = FastMapping(m)
    oi = OEM_CODES.index("OEM_D")
    em = next(e for e in emissions if (fleet.oem_idx[e.gidx] == oi).any())
    sub = em.select(fleet.oem_idx[em.gidx] == oi)
    shape, recs = encode("OEM_D", sub, ctx, d_legacy=True)
    assert shape == "OEM_C"
    raw = wrap_batch(shape, recs, 4, int(sub.ts_ms[0]))
    now = int(sub.ts_ms.max()) + 10**9
    assert fm.apply(raw, now).ok == m.apply(raw, now).ok


# ---------------------------------------------------------------- noise
def test_zero_rates_are_identity_and_flush_is_empty(fleet, routes):
    sim, _ = make_sim(fleet, routes, seed=1)
    inj = NoiseInjector(np.random.default_rng(0), ooo_p=0.0, dup_p=0.0)
    em = sim.step(60.0)
    out, is_dup, is_late = inj.apply(em, sim.t)
    assert len(out) == len(em) and not is_dup.any() and not is_late.any()
    assert (out.seq == em.seq).all()
    tail = inj.flush()
    assert tail is None or len(tail) == 0  # apply() stages an (empty) held buffer, so flush may return it


def test_noise_decisions_are_deterministic_for_same_seed(fleet, routes):
    sim, _ = make_sim(fleet, routes, seed=1)
    em = sim.step(60.0)
    outs = []
    for seed in (5, 5):
        inj = NoiseInjector(np.random.default_rng(seed))
        outs.append(inj.apply(em, sim.t))
    assert (outs[0][0].seq == outs[1][0].seq).all()
    assert (outs[0][1] == outs[1][1]).all() and (outs[0][2] == outs[1][2]).all()


def test_flush_releases_everything_and_conserves_records(fleet, routes):
    sim, _ = make_sim(fleet, routes, seed=1)
    inj = NoiseInjector(np.random.default_rng(7))
    sent = recv = 0
    for _ in range(50):
        em = sim.step(1.0)
        out, _, _ = inj.apply(em, sim.t)
        sent += len(em)
        recv += len(out)
    tail = inj.flush()
    recv += 0 if tail is None else len(tail)
    assert recv == sent + inj.stats.dup_records  # nothing lost, duplicates added
    assert inj.flush() is None  # second flush is empty


def test_duplicate_records_are_identical_copies(fleet, routes):
    sim, _ = make_sim(fleet, routes, seed=1)
    inj = NoiseInjector(np.random.default_rng(2), ooo_p=0.0, dup_p=0.3)
    seen: set[tuple[int, int]] = set()  # dup copies are held 0-5 s, so the original always sent first
    dups_found = 0
    for _ in range(30):
        out, is_dup, _ = inj.apply(sim.step(1.0), sim.t)
        for g, s, d in zip(out.gidx.tolist(), out.seq.tolist(), is_dup.tolist(), strict=True):
            if d:
                assert (g, s) in seen  # a dup is a copy of an already-sent record
                dups_found += 1
            seen.add((g, s))
    assert dups_found > 0


# ---------------------------------------------------------------- publisher / ledger
def test_ledger_conservation_with_noise_and_late_positive(fleet, routes):
    cfg = SimConfig(emit_driving_s=1, emit_idle_s=2, emit_parked_s=3, emit_charging_s=3)
    pub = SimPublisher(fleet, routes, ListTransport(), seed=3, t0_s=1_790_000_000.0, cfg=cfg,
                       noise=True, batch_retry_p=0.0)
    for i in range(30):
        pub.tick(1.0, now=float(i))
    led = pub.ledger
    assert int(led.late.sum()) > 0 and int(led.dups.sum()) > 0  # noise actually applied
    assert bool(((led.bulk + led.event) == (led.unique + led.dups)).all())
    seen = led.unique > 0
    assert (led.last_seq[seen] >= led.first_seq[seen]).all()


def test_connector_splits_shards_and_uses_envelope_shape():
    c = Connector("OEM_A")
    c.add([{"i": 0}, {"i": 1}, {"i": 2}], np.array([5, 5, 9]), "telemetry", now=0.0, shape="OEM_A")
    msgs = c.flush_due(10.0, 1000, force=True)
    got = sorted((t, len(json.loads(p)["records"])) for t, p in msgs)
    assert got == [("telemetry/OEM_A/5", 2), ("telemetry/OEM_A/9", 1)]
    assert Connector("OEM_B").flush_due(5.0, 5, force=True) == []


def test_drift_control_missing_file_is_false(tmp_path):
    assert DriftControl(tmp_path / "absent.json", 0.0).active(1.0) is False


def test_is_event_lane_critical_dtc_and_watchlist(fleet, routes):
    sim, _ = make_sim(fleet, routes, seed=1)
    em = sim.step(60.0)
    em.evt[:] = EVT["DTC_SET"]
    em.dtc[:] = DTC_INDEX["P0AA6"]  # critical EV code
    assert bool(is_event_lane(em, np.zeros(fleet.n, dtype=bool)).all())
    em.dtc[:] = DTC_INDEX["P0301"]  # valid but non-critical
    ev = is_event_lane(em, fleet.watchlist)
    assert (ev == fleet.watchlist[em.gidx]).all()


# ---------------------------------------------------------------- ground truth / ledger
def test_incident_ids_deterministic_for_same_seed(fleet, routes):
    cfg = SimConfig(emit_driving_s=60, emit_idle_s=60, emit_parked_s=60, emit_charging_s=60,
                    fault_scale=300.0, plan_days=0.4, gps_jump_p=2e-3, skew_share=0.05)
    ids = []
    for seed in (11, 11):
        sim, gt = make_sim(fleet, routes, seed=seed, cfg=cfg, t0=T_MORNING)
        for _ in range(120):
            sim.step(60.0)
        ids.append(sorted(r["incident_id"] for r in gt.table().to_pylist()))
    assert ids[0] == ids[1] and len(ids[0]) > 100


def test_fault_magnitude_ranges(fleet, routes):
    cfg = SimConfig(emit_driving_s=60, emit_idle_s=60, emit_parked_s=60, emit_charging_s=60,
                    fault_scale=300.0, plan_days=0.4, gps_jump_p=2e-3, skew_share=0.05)
    sim, gt = make_sim(fleet, routes, seed=11, cfg=cfg, t0=T_MORNING)
    for _ in range(360):
        sim.step(60.0)
    rows = gt.table().to_pylist()
    jumps = [r["value"] for r in rows if r["kind"] == G.GPS_JUMP]
    assert jumps and all(5.0 < v < 45.0 for v in jumps)  # 0.05..0.4 deg lat * 111.32 km/deg
    skews = [r["value"] for r in rows if r["kind"] == G.CLOCK_SKEW]
    assert skews and all(60_000 <= abs(v) <= 600_000 for v in skews)
    assert any(v > 0 for v in skews) and any(v < 0 for v in skews)


def test_empty_groundtruth_global_and_merge():
    g = GroundTruth(np.array(["V1"]))
    assert g.table().num_rows == 0 and g.count() == 0
    g.add_global(G.BURST, 1, 2, value=3.0, note="n")
    assert g.table().to_pylist()[0]["vin"] == "*"  # fleet-wide incidents use the sentinel vin
    g2 = GroundTruth(np.array(["V1"]))
    g2.add(G.TOW, [0], 5, 6)
    g.merge(g2)
    assert sorted(r["kind"] for r in g.table().to_pylist()) == [G.BURST, G.TOW]


def test_ledger_dup_only_records_keep_first_seq_unset():
    led = SeqLedger(np.array(["V1"]))
    idx = np.array([0, 0])
    led.record(idx, np.array([7, 7]), np.array([True, True]), np.array([False, False]), np.array([True, False]))
    assert led.table()["first_seq"].to_pylist() == [-1]  # no fresh record ever seen
    assert int(led.dups.sum()) == 2 and int(led.bulk.sum()) == 1 and int(led.event.sum()) == 1


# ---------------------------------------------------------------- behaviour / fleet / routes
def test_ts_and_seq_monotone_per_vehicle_without_skew(fleet, routes):
    sim, _ = make_sim(fleet, routes, seed=6,
                      cfg=SimConfig(emit_driving_s=60, emit_idle_s=60, emit_parked_s=60,
                                    emit_charging_s=60, skew_share=0.0, faults=False))
    last_ts: dict[int, int] = {}
    last_seq: dict[int, int] = {}
    for _ in range(60):
        em = sim.step(60.0)
        for g, ts, s in zip(em.gidx.tolist(), em.ts_ms.tolist(), em.seq.tolist(), strict=True):
            if g in last_ts:
                assert ts > last_ts[g] and s > last_seq[g]
            last_ts[g], last_seq[g] = ts, s


def test_charging_state_only_for_pure_ev(fleet, routes):
    sim, _ = make_sim(fleet, routes, seed=2, t0=T0 - 12 * 3600)
    for _ in range(24 * 30):
        em = sim.step(120.0)
        chg = em.select(em.state == 3)
        if len(chg):
            assert (fleet.powertrain[chg.gidx] == PT_EV).all()
            assert (~np.isnan(chg.soc)).all()


def test_shard_matches_partition_hash(fleet):
    assert (fleet.shard == np.array([vin_shard(v) for v in fleet.vin.tolist()])).all()


def test_driver_ref_format(fleet):
    refs = fleet.driver_ref(np.array([0, 1, 16, 255]))
    assert refs == ["drv_000000", "drv_000001", "drv_000010", "drv_0000ff"]
    assert all(re.fullmatch(r"drv_[0-9a-f]{6}", r) for r in refs)


def test_named_tenant_sizes_proportional_to_demo():
    fleet = generate_fleet(7, 2000)
    demo = yaml.safe_load(demo_tenants_path().read_text(encoding="utf-8"))
    dmap = {t["slug"]: t["vehicles"] for t in demo["tenants"]}
    fmap = {t.slug: t.size for t in fleet.tenants}
    assert not [s for s in dmap if s not in fmap]
    for slug, total in dmap.items():
        assert fmap[slug] == total * fleet.n // demo["total_vehicles"]
    assert sum(fmap.values()) == fleet.n


def test_position_negative_distance_clamps_to_start(routes):
    lat, lon = routes.position(np.array([3]), np.array([-5.0]))
    assert np.allclose(lat, routes.pts[3, 0, 0]) and np.allclose(lon, routes.pts[3, 0, 1])


# ---------------------------------------------------------------- wear / labels / features
def test_hazard_base_value_and_cap():
    assert hazard_per_day(np.array([1.0]))[0] == 0.004  # base hazard at w == 1
    assert hazard_per_day(np.array([10.0]))[0] == 0.5  # capped, never explodes


def test_repair_only_touches_mask_and_ineligible_get_nothing():
    m = WearModel(np.random.default_rng(5), 100, np.ones(100, bool))
    before = m.w.copy()
    mask = np.zeros(100, dtype=bool)
    mask[:10] = True
    m.repair(mask)
    assert (m.w[:10] < before[:10]).all() and (m.w[10:] == before[10:]).all()
    fail, _ = m.sample_breakdowns(np.random.default_rng(6), np.zeros(100, dtype=bool))
    assert not fail.any()


def test_soh_floor_after_long_decay():
    m = WearModel(np.random.default_rng(8), 200, np.ones(200, bool))
    for _ in range(400):
        m.advance_day(np.random.default_rng(9), 3.0)
    assert np.nanmin(m.soh) >= 40.0


def test_labels_earliest_wins_empty_and_horizon_edge():
    from fs_simulator.features import DAY_MS, maintenance_labels

    h = 7 * DAY_MS
    lab = maintenance_labels(np.array(["A"]), np.array([0, 0]), np.array([2000, 1500]), [1000], 7.0)
    assert lab.to_pylist()[0]["breakdown_ts_ms"] == 1500  # earliest of several wins
    empty = maintenance_labels(np.array(["A"]), np.array([], dtype=np.int64),
                               np.array([], dtype=np.int64), [1000], 7.0)
    assert empty.to_pylist()[0]["will_fail"] is False
    edge = maintenance_labels(np.array(["A", "B"]), np.array([0, 1]),
                              np.array([1000 + h, 1000 + h + 1]), [1000], 7.0)
    rows = {r["vin"]: r["will_fail"] for r in edge.to_pylist()}
    assert rows == {"A": True, "B": False}  # horizon is an inclusive upper bound


def test_build_features_empty_window_returns_empty():
    from fs_simulator.features import build_features

    t = pa.table({"vin": ["A", "B"], "ts_ms": [100, 200], "soh_pct": [90.0, 91.0], "dtc": [None, None],
                  "speed_kmh": [10.0, 20.0], "odo_km": [5.0, 6.0], "evt": ["POSITION", "POSITION"],
                  "state": [1, 1]})
    f = build_features(t, 50)  # prediction time before all rows
    assert len(f["vin"]) == 0


def test_features_single_point_nans_and_idle_share():
    from fs_simulator.features import build_features

    t = pa.table({"vin": ["A"], "ts_ms": [100], "soh_pct": [90.0], "dtc": [None],
                  "speed_kmh": [10.0], "odo_km": [9.0], "evt": ["POSITION"], "state": [2]})
    f = build_features(t, 1000)
    assert np.isnan(f["soh_slope_per_day"][0])  # < 2 points => no slope
    assert np.isnan(f["hours_since_last_dtc"][0])  # no DTC => undefined, not zero
    assert f["idle_share"][0] == 1.0 and f["km_driven"][0] == 0.0 and f["soh_last"][0] == 90.0

def test_km_driven_is_time_ordered_and_rollback_clamped():
    # km_driven = last(odo) - first(odo) in time order, clamped >= 0: an odometer
    # rollback must not inflate distance (integrator fix; was max-min before).
    from fs_simulator.features import build_features

    def table(odo):
        return pa.table({"vin": ["A", "A"], "ts_ms": [100, 200],
                         "soh_pct": [90.0, 90.0], "dtc": [None, None],
                         "speed_kmh": [10.0, 10.0], "odo_km": odo,
                         "evt": ["POSITION", "POSITION"], "state": [1, 1]})

    assert build_features(table([4.0, 9.0]), 1000)["km_driven"][0] == 5.0
    assert build_features(table([9.0, 4.0]), 1000)["km_driven"][0] == 0.0


@given(seed=st.integers(0, 10_000))
@settings(max_examples=15, deadline=None)
def test_features_invariant_to_post_prediction_tamper_of_any_column(seed):
    from fs_simulator.features import DAY_MS, build_features

    rng = np.random.default_rng(seed)
    n = 60
    ts = np.sort(rng.integers(0, 10 * DAY_MS, n))
    p = 5 * DAY_MS
    cols = {"vin": [f"V{i}" for i in rng.integers(0, 3, n)], "ts_ms": ts,
            "soh_pct": rng.uniform(80, 100, n).astype(np.float32),
            "dtc": [None] * n, "speed_kmh": rng.uniform(0, 60, n).astype(np.float32),
            "odo_km": np.cumsum(rng.uniform(0, 1, n)).astype(np.float32),
            "evt": ["POSITION"] * n, "state": rng.integers(0, 4, n).astype(np.int8)}
    t = pa.table(cols)
    base = build_features(t, p)
    future = ts >= p
    tampered = t.set_column(4, "speed_kmh",
                            pa.array(np.where(future, 999.0, cols["speed_kmh"]).astype(np.float32)))
    tampered = tampered.set_column(5, "odo_km",
                                   pa.array(np.where(future, 0.0, cols["odo_km"]).astype(np.float32)))
    other = build_features(tampered, p)
    assert set(other) == set(base)
    for k in base:
        a, b = np.asarray(base[k]), np.asarray(other[k])
        assert a.shape == b.shape and (a == b).all() or np.allclose(a.astype(float), b.astype(float), equal_nan=True)


# ---------------------------------------------------------------- sim KEK edges
def test_sim_pii_version_aad_and_tamper_raise():
    dek = new_deks(1, np.random.default_rng(7))[0]
    blob = encrypt_pii(dek, "s", "hi", nonce=b"0" * 12)
    with pytest.raises(ValueError, match="version"):
        decrypt_pii(dek, "s", b"\x02" + blob[1:])
    with pytest.raises(Exception):
        decrypt_pii(dek, "other-subject", blob)
    with pytest.raises(Exception):
        decrypt_pii(dek, "s", blob[:-1] + bytes([blob[-1] ^ 1]))


def test_sim_local_unwrap_rejects_garbage():
    w = LocalDevKek()
    with pytest.raises(Exception):
        w.unwrap(b"too-short")
    with pytest.raises(Exception):
        w.unwrap(b"local-dev-kek:v1:!!!not-base64!!!")


def test_sim_new_deks_and_empty_wrap():
    a = new_deks(2, np.random.default_rng(7))
    b = new_deks(2, np.random.default_rng(7))
    assert [len(d) for d in a] == [32, 32] and a == b  # rng only for reproducible tests
    assert LocalDevKek().wrap_many([]) == []
    assert [len(d) for d in new_deks(3)] == [32, 32, 32]


def test_sim_vault_batches_are_counted_ordered_and_unique(monkeypatch):
    import sys
    import types

    seen = {"batches": 0}

    class FakeTransit:
        def encrypt_data(self, name, plaintext, batch_input):
            seen["batches"] += 1
            assert name == "fs-kek"
            base = seen["batches"] * 100_000
            return {"data": {"batch_results": [{"ciphertext": f"vault:v1:{base + i}"}
                                               for i, _ in enumerate(batch_input)]}}

    fake_hvac = types.ModuleType("hvac")
    fake_hvac.Client = lambda **kw: types.SimpleNamespace(
        secrets=types.SimpleNamespace(transit=FakeTransit()), is_authenticated=lambda: True)
    monkeypatch.setitem(sys.modules, "hvac", fake_hvac)
    from fs_simulator.crypto import VaultTransitKek, make_wrapper

    w = make_wrapper(local=False)
    assert isinstance(w, VaultTransitKek)
    deks = [bytes([i % 256]) * 32 for i in range(1200)]
    out = w.wrap_many(deks)
    assert len(out) == 1200 and len(set(out)) == 1200  # one distinct ciphertext per DEK, order kept
    assert seen["batches"] == 3 and w.calls == 3
