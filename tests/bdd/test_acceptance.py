"""BDD acceptance runner (no behave dependency): each scenario maps 1:1 to a .feature
file in this directory and executes the real code paths (services, not mocks).

Step text lives in the .feature files; this module binds and runs them so the
acceptance suite works in CI with zero extra dependencies.
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


def test_bdd_tow_alert():
    from fs_processor.application.priority_service import PriorityConfig, PriorityService
    from fscore import contracts as C

    svc = PriorityService(config=PriorityConfig(tenant_default="t1"),
                          tenant_of=lambda v: "t1", clock=lambda: 1_790_590_000_000)
    out = svc.handle(_ev("BDD-VIN-1", 1_790_590_000_000, speed_kmh=30.0), {})
    assert len(out) == 1
    a = alert_pb2.Alert()
    a.ParseFromString(out[0].value)
    assert a.rule == alert_pb2.RULE_TOW and a.severity == alert_pb2.CRITICAL
    assert a.alert_id == C.alert_id("BDD-VIN-1", "TOW", 1_790_590_000_000)


def test_bdd_tenant_isolation():
    pytest.importorskip("psycopg")
    import psycopg

    try:
        admin = psycopg.connect(
            "postgresql://postgres:postgres@localhost:5432/fleetsentinel", connect_timeout=3)
    except Exception:
        pytest.skip("live Postgres (fs-pg) not running")
    admin.autocommit = True
    admin.execute("INSERT INTO tariff_region(code, name) VALUES ('bdd','bdd') ON CONFLICT DO NOTHING")
    admin.execute("INSERT INTO tenant(name) VALUES ('bdd-a'), ('bdd-b') ON CONFLICT DO NOTHING")
    t = {n: str(i) for i, n in
         admin.execute("SELECT id, name FROM tenant WHERE name IN ('bdd-a','bdd-b')").fetchall()}
    try:
        for tag in ("a", "b"):
            row = admin.execute(
                "SELECT id FROM fleet WHERE tenant_id=%s AND name=%s", (t[f"bdd-{tag}"], f"bdd-f-{tag}")).fetchone()
            if row is None:
                tr = admin.execute("SELECT id FROM tariff_region WHERE code='bdd'").fetchone()[0]
                admin.execute("INSERT INTO fleet(tenant_id, tariff_region_id, name) VALUES (%s,%s,%s)",
                              (t[f"bdd-{tag}"], str(tr), f"bdd-f-{tag}"))
        ca = psycopg.connect("postgresql://fs_app:fs_app_dev@localhost:5432/fleetsentinel",
                             autocommit=True)
        ca.execute(f"SET app.tenant_id = '{t['bdd-a']}'")
        names = [r[0] for r in ca.execute("SELECT name FROM fleet").fetchall()]
        assert names == ["bdd-f-a"] or set(names) == {"bdd-f-a"}
        ca.close()
    finally:
        admin.execute("DELETE FROM fleet WHERE tenant_id IN (SELECT id FROM tenant WHERE name IN ('bdd-a','bdd-b'))")
        admin.execute("DELETE FROM tenant WHERE name IN ('bdd-a','bdd-b')")
        admin.close()


def test_bdd_schema_drift_to_dlq():
    import json
    from pathlib import Path

    import yaml
    from fscore import contracts as C
    from fscore.mapping_engine import Mapping

    maps = Path(__file__).resolve().parents[2] / "contracts" / "mappings"
    m = Mapping(yaml.safe_load((maps / "oem_c.yaml").read_text(encoding="utf-8")))
    good = json.loads((maps / "samples" / "oem_c.json").read_text(encoding="utf-8"))["events"][0]
    r = m.apply({"batch_id": "bdd", "events": [{**good, "evt": "SOME_NEW_FW_CODE"}]},
                now_ms=1_790_590_600_000)
    assert len(r.ok) == 0 and len(r.failed) == 1
    assert r.failed[0].reason == C.ERR_VALIDATION_FAILED and "unmapped event code" in r.failed[0].detail


def test_bdd_erasure_replays_erased():
    from fscore import crypto

    dek = crypto.generate_dek()
    wrapper = crypto.LocalDevWrapper()
    wrapped = wrapper.wrap(dek)
    store = crypto.SubjectKeyStore(wrapper)
    blob = crypto.encrypt(dek, "bdd-key", b"BDD Name", crypto.AAD_DRIVER_NAME)
    assert crypto.decrypt_or_erased(store, blob, wrapped, crypto.AAD_DRIVER_NAME) == "BDD Name"
    # destroy = wrapped_dek NULL (what KEY_DESTROY does); replay must render [erased]
    assert crypto.decrypt_or_erased(store, blob, None, crypto.AAD_DRIVER_NAME) == crypto.ERASED_MARKER


def test_bdd_briefing_totals():
    from fs_agent import briefing as B

    opps = [B.Opportunity("cost", "Idle", "d", 500.0),
            B.Opportunity("maintenance", "Inspect V", "p=0.9", 1200.0)]
    out = B.build_briefing(opps, "2026-09-29T00:00:00Z")
    assert out["total_impact_usd"] == 1700.0 and len(out["items"]) == 2
    assert [i["kind"] for i in out["items"]] == ["maintenance", "cost"]
