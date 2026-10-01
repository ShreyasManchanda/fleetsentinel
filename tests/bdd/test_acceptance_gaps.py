"""BDD fidelity gaps: assertions the .feature files promise but test_acceptance.py
does not pin down (no behave dependency; real code paths, same style).

- schema_drift.feature says "OEM_B record" (existing test only covers OEM_C).
- tow_alert.feature says the alert id "is deterministic for the same input"
  (existing test derives the id once via the same helper instead of replaying).
- tenant_isolation.feature says tenant A "looks up bdd-b's vehicle by VIN"
  (existing test only lists fleets) — needs live fs-pg, skips cleanly without it.
- briefing.feature says "maintenance outranks cost" as a ranking property.
"""
from __future__ import annotations

import pytest
from fleetsentinel.v1 import alert_pb2, telemetry_pb2


def _ev(vin, ts, **kw):
    d = {"vin": vin, "ts_ms": ts, "lat": 12.97, "lon": 77.59, "speed_kmh": 0.0,
         "ignition": False, "odo_km": 1.0, "oem": "OEM_A", "ingest_ts_ms": ts + 50}
    d.update(kw)
    e = telemetry_pb2.TelemetryEvent()
    for k, v in d.items():
        if k == "dtc":
            e.dtc.extend(v)
        else:
            setattr(e, k, v)
    return e


def test_bdd_schema_drift_oem_b_unknown_code_to_dlq():
    """schema_drift.feature names an OEM_B record: unknown code must fail, no POSITION."""
    import json
    from pathlib import Path

    import yaml
    from fleetsentinel.v1 import telemetry_pb2 as tpb
    from fscore import contracts as C
    from fscore.mapping_engine import Mapping

    maps = Path(__file__).resolve().parents[2] / "contracts" / "mappings"
    m = Mapping(yaml.safe_load((maps / "oem_b.yaml").read_text(encoding="utf-8")))
    batch = json.loads((maps / "samples" / "oem_b.json").read_text(encoding="utf-8"))
    rec = dict(batch["d"][0])
    rec["ev"] = "SOME_NEW_FW_CODE"
    r = m.apply({"bid": "bdd-oem-b", "d": [rec]}, now_ms=1_790_590_600_000)
    assert len(r.ok) == 0 and len(r.failed) == 1
    assert r.failed[0].reason == C.ERR_VALIDATION_FAILED
    assert "SOME_NEW_FW_CODE" in r.failed[0].detail
    # no fabricated POSITION event for the drifted record
    assert all(e.evt != tpb.POSITION for e in r.ok)


def test_bdd_tow_alert_id_deterministic_across_replays():
    """Same tow input on fresh lanes (restart/replay) yields byte-identical alert ids."""
    from fs_processor.application.priority_service import PriorityConfig, PriorityService

    def fire():
        svc = PriorityService(config=PriorityConfig(tenant_default="t1"),
                              tenant_of=lambda v: "t1", clock=lambda: 1_790_590_000_000)
        out = svc.handle(_ev("BDD-VIN-1", 1_790_590_000_000, speed_kmh=30.0), {})
        assert len(out) == 1
        a = alert_pb2.Alert()
        a.ParseFromString(out[0].value)
        return a.alert_id

    first, second = fire(), fire()
    assert first and first == second


def test_bdd_briefing_ranked_descending_by_impact():
    """briefing.feature: maintenance outranks cost — ranking is a sorted property."""
    from fs_agent import briefing as B

    opps = [B.Opportunity("cost", "Idle", "d", 500.0),
            B.Opportunity("ops", "Tiny", "d", 50.0),
            B.Opportunity("maintenance", "Inspect V", "p=0.9", 1200.0)]
    out = B.build_briefing(opps, "2026-09-29T00:00:00Z")
    impacts = [i["impact_usd"] for i in out["items"]]
    assert impacts == sorted(impacts, reverse=True)
    assert [i["kind"] for i in out["items"]][0] == "maintenance"
    assert out["total_impact_usd"] == pytest.approx(1750.0)


def test_bdd_cross_tenant_vin_lookup_invisible():
    """tenant_isolation.feature: tenant A looking up B's VIN finds nothing (live pg)."""
    pytest.importorskip("psycopg")
    import random

    import psycopg
    from fscore.vin import make_vin

    try:
        admin = psycopg.connect(
            "postgresql://postgres:postgres@localhost:5432/fleetsentinel", connect_timeout=3)
    except Exception:
        pytest.skip("live Postgres (fs-pg) not running")
    admin.autocommit = True
    rng = random.Random(20261001)
    try:
        admin.execute("INSERT INTO tariff_region(code, name) VALUES ('bdd2','bdd2') ON CONFLICT DO NOTHING")
        admin.execute("INSERT INTO tenant(name) VALUES ('bdd2-a'), ('bdd2-b') ON CONFLICT DO NOTHING")
        t = {n: str(i) for i, n in
             admin.execute("SELECT id, name FROM tenant WHERE name IN ('bdd2-a','bdd2-b')").fetchall()}
        vins = {}
        for tag in ("a", "b"):
            row = admin.execute(
                "SELECT id FROM fleet WHERE tenant_id=%s AND name=%s", (t[f"bdd2-{tag}"], f"bdd2-f-{tag}")).fetchone()
            if row is None:
                tr = admin.execute("SELECT id FROM tariff_region WHERE code='bdd2'").fetchone()[0]
                row = admin.execute(
                    "INSERT INTO fleet(tenant_id, tariff_region_id, name) VALUES (%s,%s,%s) RETURNING id",
                    (t[f"bdd2-{tag}"], str(tr), f"bdd2-f-{tag}")).fetchone()
            fid = row[0]
            admin.execute("INSERT INTO oem(code, name) VALUES ('bdd2-oem','bdd2-oem') ON CONFLICT DO NOTHING")
            oem = admin.execute("SELECT id FROM oem WHERE code='bdd2-oem'").fetchone()[0]
            admin.execute(
                "INSERT INTO vehicle_model(oem_id, name, powertrain) VALUES (%s,'bdd2-m','ICE') ON CONFLICT DO NOTHING",
                (str(oem),))
            model = admin.execute("SELECT id FROM vehicle_model WHERE name='bdd2-m'").fetchone()[0]
            vin = make_vin("1HG", rng)
            admin.execute("INSERT INTO vehicle(tenant_id, fleet_id, model_id, vin) VALUES (%s,%s,%s,%s)",
                          (t[f"bdd2-{tag}"], str(fid), str(model), vin))
            vins[tag] = vin
        for tag, other in (("a", "b"), ("b", "a")):
            c = psycopg.connect("postgresql://fs_app:fs_app_dev@localhost:5432/fleetsentinel",
                                autocommit=True)
            try:
                c.execute(f"SET app.tenant_id = '{t[f'bdd2-{tag}']}'")
                own = c.execute("SELECT vin FROM vehicle WHERE vin = %s", (vins[tag],)).fetchall()
                assert len(own) == 1
                cross = c.execute("SELECT vin FROM vehicle WHERE vin = %s", (vins[other],)).fetchall()
                assert cross == [], f"tenant {tag} sees VIN of tenant {other}"
            finally:
                c.close()
    finally:
        admin.execute("DELETE FROM vehicle WHERE tenant_id IN (SELECT id FROM tenant WHERE name IN ('bdd2-a','bdd2-b'))")
        admin.execute("DELETE FROM fleet WHERE tenant_id IN (SELECT id FROM tenant WHERE name IN ('bdd2-a','bdd2-b'))")
        admin.execute("DELETE FROM tenant WHERE name IN ('bdd2-a','bdd2-b')")
        admin.close()
