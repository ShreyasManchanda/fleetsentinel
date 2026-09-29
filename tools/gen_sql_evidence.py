"""One-off evidence generator for the SQL write-up (spec 9.2, case 1).

Seeds ~200k alerts + captures EXPLAIN ANALYZE before/after the partial index.
Safe to re-run (idempotent per tenant name). Run: uv run --frozen python tools/gen_sql_evidence.py
"""
from __future__ import annotations

import random
import time
import uuid
from pathlib import Path

import psycopg

ADMIN = "postgresql://postgres:postgres@localhost:5432/fleetsentinel"
TENANT = "sql-ev-a"
N_VEHICLES = 2000
N_ALERTS = 200_000


def main() -> None:
    rng = random.Random(20260929)  # noqa: S311 - synthetic evidence data, not cryptographic use
    c = psycopg.connect(ADMIN, autocommit=True)
    c.execute("INSERT INTO tariff_region(code, name) VALUES ('sql-ev','sql-ev') ON CONFLICT DO NOTHING")
    c.execute("INSERT INTO tenant(name) VALUES (%s) ON CONFLICT DO NOTHING", (TENANT,))
    tid = c.execute("SELECT id FROM tenant WHERE name=%s", (TENANT,)).fetchone()[0]
    c.execute("INSERT INTO oem(code, name) VALUES ('sql-oem','sql-oem') ON CONFLICT DO NOTHING")
    oem = c.execute("SELECT id FROM oem WHERE code='sql-oem'").fetchone()[0]
    c.execute(
        "INSERT INTO vehicle_model(oem_id, name, powertrain) VALUES (%s,'sql-m','ICE') ON CONFLICT DO NOTHING",
        (str(oem),))
    model = c.execute("SELECT id FROM vehicle_model WHERE name='sql-m'").fetchone()[0]
    tr = c.execute("SELECT id FROM tariff_region WHERE code='sql-ev'").fetchone()[0]
    row = c.execute("SELECT id FROM fleet WHERE tenant_id=%s AND name='sql-f'", (str(tid),)).fetchone()
    if row is None:
        fid = c.execute(
            "INSERT INTO fleet(tenant_id, tariff_region_id, name) VALUES (%s,%s,'sql-f') RETURNING id",
            (str(tid), str(tr))).fetchone()[0]
    else:
        fid = row[0]
    from fscore.vin import make_vin

    vrng = random.Random(11)  # noqa: S311 - synthetic VINs for evidence, not secrets
    vins = [make_vin("1HG", vrng) for _ in range(N_VEHICLES)]
    with c.cursor() as cur:
        cur.executemany(
            "INSERT INTO vehicle(tenant_id, fleet_id, model_id, vin) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            [(str(tid), str(fid), str(model), v) for v in vins])
    vids = [r[0] for r in c.execute("SELECT id FROM vehicle WHERE tenant_id=%s", (str(tid),)).fetchall()]
    have = c.execute("SELECT count(*) FROM alert WHERE tenant_id=%s", (str(tid),)).fetchone()[0]
    if have < N_ALERTS:
        need = N_ALERTS - have
        t0 = time.perf_counter()
        rows = []
        for _ in range(need):
            r = rng.random()
            status = "open" if r < 0.5 else ("ack" if r < 0.7 else "resolved")
            day = rng.randint(0, 29)
            rows.append((str(uuid.uuid4()), str(tid), str(rng.choice(vids)), "TOW" if r < 0.1 else "EXCESSIVE_IDLING",
                         "WARNING", status, f"2026-09-{1 + day % 28:02d}T{rng.randint(0, 23):02d}:00:00Z"))
            if len(rows) == 5000:
                with c.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO alert(id, tenant_id, vehicle_id, rule_code, severity, status, opened_at)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s)", rows)
                rows = []
        if rows:
            with c.cursor() as cur:
                cur.executemany(
                    "INSERT INTO alert(id, tenant_id, vehicle_id, rule_code, severity, status, opened_at)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s)", rows)
        print(f"inserted {need} alerts in {time.perf_counter() - t0:.1f}s")
    c.execute("ANALYZE alert")
    q = ("EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) SELECT id, vehicle_id, rule_code, opened_at FROM alert "
         "WHERE tenant_id = %s AND status IN ('open','ack') ORDER BY opened_at DESC LIMIT 50")
    after = "\n".join(r[0] for r in c.execute(q, (str(tid),)).fetchall())
    c.execute("DROP INDEX alert_open_queue")
    c.execute("ANALYZE alert")
    before = "\n".join(r[0] for r in c.execute(q, (str(tid),)).fetchall())
    c.execute("CREATE INDEX alert_open_queue ON alert (tenant_id, status, opened_at DESC)"
              " WHERE status IN ('open','ack')")
    c.execute("ANALYZE alert")
    out = Path("docs/evidence/sql/case1-open-alerts.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    n = c.execute("SELECT count(*) FROM alert WHERE tenant_id=%s", (str(tid),)).fetchone()[0]
    out.write_text(
        "# Case 1: open-alert queue per tenant (partial composite index)\n\n"
        f"Tenant `sql-ev-a`, {n:,} alert rows. Query: open/ack alerts, newest first, LIMIT 50.\n\n"
        "## BEFORE (no `alert_open_queue` index)\n\n```\n" + before + "\n```\n\n"
        "## AFTER (`CREATE INDEX alert_open_queue ON alert (tenant_id, status, opened_at DESC)"
        " WHERE status IN ('open','ack')`)\n\n```\n" + after + "\n```\n",
        encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
