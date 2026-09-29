"""Live-Postgres security proofs (reviewer HIGH #1/#3; closes B-019 permanently).

Requires the ``fs-pg`` container (``docker start fs-pg`` + ``python tools/fs.py migrate``).
Skips cleanly when Postgres is unreachable so unit runs stay green without Docker.
"""
from __future__ import annotations

import random
import re

import pytest

psycopg = pytest.importorskip("psycopg")

ADMIN_DSN = "postgresql://postgres:postgres@localhost:5432/fleetsentinel"
APP_DSN = "postgresql://fs_app:fs_app_dev@localhost:5432/fleetsentinel"


def _pg_available() -> bool:
    try:
        c = psycopg.connect(ADMIN_DSN, connect_timeout=3)
        c.close()
        return True
    except Exception:
        return False


needs_pg = pytest.mark.skipif(not _pg_available(), reason="live Postgres (fs-pg) not running")


def _sa_to_py(sql: str) -> str:
    """SQLAlchemy :named -> psycopg %(named)s. Real SQL text from fs_api.adapters.pg_sql."""
    return re.sub(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)", r"%(\1)s", sql)


@pytest.fixture()
def db():
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    admin.execute("INSERT INTO tariff_region(code, name) VALUES ('it-ten', 'it-ten') ON CONFLICT DO NOTHING")
    admin.execute("INSERT INTO tenant(name) VALUES ('it-a'), ('it-b') ON CONFLICT DO NOTHING")
    yield admin
    admin.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_truncate")
    admin.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update")
    try:
        admin.execute("DELETE FROM audit_log WHERE tenant_id IN (SELECT id FROM tenant WHERE name IN ('it-a','it-b'))")
    finally:
        admin.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update")
        admin.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_truncate")
    admin.execute("DELETE FROM driver WHERE tenant_id IN (SELECT id FROM tenant WHERE name IN ('it-a','it-b'))")
    admin.execute("DELETE FROM vehicle WHERE tenant_id IN (SELECT id FROM tenant WHERE name IN ('it-a','it-b'))")
    admin.execute("DELETE FROM fleet WHERE tenant_id IN (SELECT id FROM tenant WHERE name IN ('it-a','it-b'))")
    admin.execute("DELETE FROM subject_key WHERE tenant_id IN (SELECT id FROM tenant WHERE name IN ('it-a','it-b'))")
    admin.execute("DELETE FROM tenant WHERE name IN ('it-a','it-b')")
    admin.execute("DELETE FROM tariff_region WHERE code = 'it-ten'")
    admin.close()


def _tenants(admin):
    rows = admin.execute("SELECT id, name FROM tenant WHERE name IN ('it-a','it-b')").fetchall()
    return {n: str(i) for i, n in rows}


def _app(tenant_id: str):
    c = psycopg.connect(APP_DSN, autocommit=True)
    c.execute(f"SET app.tenant_id = '{tenant_id}'")
    return c


@needs_pg
def test_bola_cross_tenant_vehicle_and_fleet_invisible(db):
    """Tenant B must not see tenant A's fleet rows or vehicle-by-VIN (BOLA)."""
    from fscore.vin import make_vin

    admin = db
    t = _tenants(admin)
    tr = admin.execute("SELECT id FROM tariff_region WHERE code='it-ten'").fetchone()[0]
    admin.execute(
        "INSERT INTO oem(code, name) VALUES ('it-oem','it-oem') ON CONFLICT DO NOTHING")
    oem = admin.execute("SELECT id FROM oem WHERE code='it-oem'").fetchone()[0]
    admin.execute(
        "INSERT INTO vehicle_model(oem_id, name, powertrain) VALUES (%s,'it-m','ICE') ON CONFLICT DO NOTHING",
        (str(oem),))
    model = admin.execute("SELECT id FROM vehicle_model WHERE name='it-m'").fetchone()[0]
    vins = {}
    rng = random.Random(20260929)
    for tag in ("a", "b"):
        tid = t[f"it-{tag}"]
        fid = admin.execute(
            "INSERT INTO fleet(tenant_id, tariff_region_id, name) VALUES (%s,%s,%s) RETURNING id",
            (tid, str(tr), f"it-f-{tag}")).fetchone()[0]
        vin = make_vin("1HG", rng)
        admin.execute(
            "INSERT INTO vehicle(tenant_id, fleet_id, model_id, vin) VALUES (%s,%s,%s,%s)",
            (tid, str(fid), str(model), vin))
        vins[tag] = vin
    # fleet list scoped
    for tag, other in (("a", "b"), ("b", "a")):
        c = _app(t[f"it-{tag}"])
        names = [r[0] for r in c.execute("SELECT name FROM fleet").fetchall()]
        assert names == [f"it-f-{tag}"], names
        # vehicle by own VIN visible, other's VIN invisible
        own = c.execute("SELECT vin FROM vehicle WHERE vin = %s", (vins[tag],)).fetchall()
        assert len(own) == 1
        cross = c.execute("SELECT vin FROM vehicle WHERE vin = %s", (vins[other],)).fetchall()
        assert cross == []
        c.close()


@needs_pg
def test_telemetry_base_table_denied_but_view_allowed(db):
    """B-006 mitigation: fs_app cannot touch the hypertable, only the security-barrier view."""
    from psycopg import errors

    t = _tenants(db)
    c = _app(t["it-a"])
    with pytest.raises(errors.InsufficientPrivilege):
        c.execute("SELECT * FROM telemetry_1m LIMIT 1").fetchall()
    c.execute("SELECT * FROM v_telemetry_1m LIMIT 1").fetchall()  # must not raise
    c.close()


@needs_pg
def test_audit_chain_verifies_then_detects_tamper(db):
    """audit_verify() is true on an intact chain and names the first tampered row."""
    admin = db
    t = _tenants(admin)
    for i in range(3):
        admin.execute(
            "INSERT INTO audit_log(ts, tenant_id, actor_id, action) VALUES (now(), %s, 'it', %s)",
            (t["it-a"], f"it.action.{i}"))
    ok, checked, bad = admin.execute("SELECT * FROM audit_verify()").fetchone()
    assert ok is True and checked == 3 and bad is None
    seq = admin.execute(
        "SELECT seq FROM audit_log WHERE tenant_id = %s ORDER BY seq LIMIT 1 OFFSET 1",
        (t["it-a"],)).fetchone()[0]
    admin.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update")
    try:
        admin.execute("UPDATE audit_log SET action = 'it.tampered' WHERE seq = %s", (seq,))
    finally:
        admin.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update")
    ok, checked, bad = admin.execute("SELECT * FROM audit_verify()").fetchone()
    assert ok is False and bad == seq


@needs_pg
def test_erasure_replay_renders_erased_marker(db):
    """DB-level crypto-shredding: run the REAL DSR SQL (KEY_DESTROY/KEY_READ/DRIVER_NULL_PII
    from fs_api.adapters.pg_sql), then decrypt_or_erased must render [erased], not plaintext."""
    from fs_api.adapters import pg_sql as q
    from fscore import crypto

    admin = db
    t = _tenants(admin)
    tid = t["it-a"]
    fid = admin.execute(
        "SELECT id FROM fleet WHERE tenant_id = %s", (tid,)).fetchone()
    if fid is None:
        tr = admin.execute("SELECT id FROM tariff_region WHERE code='it-ten'").fetchone()[0]
        fid = admin.execute(
            "INSERT INTO fleet(tenant_id, tariff_region_id, name) VALUES (%s,%s,'it-f-a') RETURNING id",
            (tid, str(tr))).fetchone()[0]
    else:
        fid = fid[0]
    key_id, dek = "it-key-1", crypto.generate_dek()
    wrapper = crypto.LocalDevWrapper()  # DEV-ONLY wrapper: proves the DB mechanics + replay rendering, not KEK security
    wrapped = wrapper.wrap(dek)
    admin.execute(
        "INSERT INTO subject_key(subject_key_id, tenant_id, wrapped_dek) VALUES (%s,%s,%s)",
        (key_id, tid, wrapped))
    name_ct = crypto.encrypt(dek, key_id, b"Ada Lovelace", crypto.AAD_DRIVER_NAME)
    did = admin.execute(
        "INSERT INTO driver(tenant_id, fleet_id, name_enc, subject_key_id) VALUES (%s,%s,%s,%s) RETURNING id",
        (tid, str(fid), name_ct, key_id)).fetchone()[0]

    c = _app(tid)
    store = crypto.SubjectKeyStore(wrapper)
    cur = c.execute(_sa_to_py(q.DSR_SUBJECT), {"id": str(did)})
    cols = [d.name for d in cur.description]
    subj = dict(zip(cols, cur.fetchone(), strict=True))
    assert crypto.decrypt_or_erased(store, subj["name_enc"], subj["wrapped_dek"],
                                    crypto.AAD_DRIVER_NAME) == "Ada Lovelace"
    # destroy via the REAL SQL, then null PII via the REAL SQL
    cur = c.execute(_sa_to_py(q.KEY_DESTROY), {"key": key_id})
    assert cur.rowcount == 1
    c.execute(_sa_to_py(q.DRIVER_NULL_PII), {"id": str(did)})
    cur = c.execute(_sa_to_py(q.KEY_READ), {"key": key_id})
    acols = [d.name for d in cur.description]
    keyrow = dict(zip(acols, cur.fetchone(), strict=True))
    assert keyrow["wrapped_dek"] is None and keyrow["destroyed_at"] is not None
    # replay: previously captured ciphertext now renders [erased]
    assert crypto.decrypt_or_erased(store, name_ct, keyrow["wrapped_dek"],
                                    crypto.AAD_DRIVER_NAME) == crypto.ERASED_MARKER
    c.close()
