"""Evidence for SQL write-up cases 2-4 (spec 9.2). Re-runnable, idempotent per tenant.

Case 2: fleet cost summary — full aggregation vs materialized view.
Case 3: trip history deep page — OFFSET vs keyset.
Case 4: vehicle list with alert counts — ORM N+1 vs single join (query count + latency).
Run: uv run --frozen python tools/gen_sql_evidence2.py
"""
from __future__ import annotations

import random
import time
import uuid
from pathlib import Path

import psycopg

ADMIN = "postgresql://postgres:postgres@localhost:5432/fleetsentinel"
TENANT = "sql-ev-a"
N_TRIPS = 120_000


def _plan(c, sql, params=()) -> str:
    return "\n".join(r[0] for r in c.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) " + sql, params).fetchall())


def main() -> None:
    rng = random.Random(4242)
    c = psycopg.connect(ADMIN, autocommit=True)
    tid = c.execute("SELECT id FROM tenant WHERE name=%s", (TENANT,)).fetchone()[0]
    vids = [r[0] for r in c.execute("SELECT id FROM vehicle WHERE tenant_id=%s", (str(tid),)).fetchall()]
    have = c.execute("SELECT count(*) FROM trip WHERE tenant_id=%s", (str(tid),)).fetchone()[0]
    if have < N_TRIPS:
        need, t0 = N_TRIPS - have, time.perf_counter()
        rows = []
        for _ in range(need):
            v = rng.choice(vids)
            d, h = rng.randint(0, 29), rng.randint(0, 23)
            rows.append((str(uuid.uuid4()), str(tid), str(v),
                         f"2026-09-{1 + d % 28:02d}T{h:02d}:00:00Z",
                         f"2026-09-{1 + d % 28:02d}T{h:02d}:30:00Z",
                         round(rng.uniform(1, 120), 2), rng.randint(0, 3600),
                         round(rng.uniform(0, 30), 2), round(rng.uniform(0, 25), 2),
                         rng.randint(0, 9), rng.randint(0, 9)))
            if len(rows) == 5000:
                with c.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO trip(id, tenant_id, vehicle_id, start_ts, end_ts, distance_km,"
                        " idle_s, energy_used, energy_cost, harsh_brake_count, harsh_accel_count)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
                rows = []
        if rows:
            with c.cursor() as cur:
                cur.executemany(
                    "INSERT INTO trip(id, tenant_id, vehicle_id, start_ts, end_ts, distance_km,"
                    " idle_s, energy_used, energy_cost, harsh_brake_count, harsh_accel_count)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
        print(f"inserted {need} trips in {time.perf_counter() - t0:.1f}s")
    c.execute("ANALYZE trip")
    n = c.execute("SELECT count(*) FROM trip WHERE tenant_id=%s", (str(tid),)).fetchone()[0]
    out = Path("docs/evidence/sql")
    out.mkdir(parents=True, exist_ok=True)

    # ---- case 2: cost summary over all trips vs the materialized view
    q2 = ("SELECT date_trunc('day', start_ts) AS day, sum(distance_km), sum(energy_cost)"
          " FROM trip WHERE tenant_id = %s GROUP BY 1 ORDER BY 1")
    before2 = _plan(c, q2, (str(tid),))
    t0 = time.perf_counter()
    c.execute("REFRESH MATERIALIZED VIEW mv_fleet_cost_daily")
    refresh_ms = (time.perf_counter() - t0) * 1000
    q2b = ("SELECT day, sum(distance_km), sum(energy_cost) FROM mv_fleet_cost_daily"
           " WHERE tenant_id = %s GROUP BY 1 ORDER BY 1")
    after2 = _plan(c, q2b, (str(tid),))
    (out / "case2-cost-summary.md").write_text(
        "# Case 2: fleet cost summary — full aggregation vs materialized view\n\n"
        f"Tenant `sql-ev-a`, {n:,} trip rows. Refresh took {refresh_ms:.0f} ms (scheduled, off-request).\n\n"
        "## BEFORE (aggregate ~120k trip rows per request)\n\n```\n" + before2 + "\n```\n\n"
        "## AFTER (read pre-aggregated daily rows)\n\n```\n" + after2 + "\n```\n", encoding="utf-8")

    # ---- case 3: deep page — OFFSET vs keyset
    q3off = ("SELECT id, start_ts FROM trip WHERE tenant_id = %s ORDER BY start_ts DESC, id DESC"
             " LIMIT 50 OFFSET 100000")
    q3key = ("SELECT id, start_ts FROM trip WHERE tenant_id = %s AND (start_ts, id) < (%s, %s)"
             " ORDER BY start_ts DESC, id DESC LIMIT 50")
    anchor = c.execute(
        "SELECT start_ts, id FROM trip WHERE tenant_id=%s ORDER BY start_ts DESC, id DESC LIMIT 1 OFFSET 100000",
        (str(tid),)).fetchone()
    before3 = _plan(c, q3off, (str(tid),))
    after3 = _plan(c, q3key, (str(tid), str(anchor[0]), str(anchor[1])))
    (out / "case3-keyset-pagination.md").write_text(
        "# Case 3: trip history deep page — OFFSET vs keyset\n\n"
        "Page at depth 100,000 (50 rows). Keyset cursor = `(start_ts, id)`.\n\n"
        "## BEFORE (OFFSET scans and discards 100k rows)\n\n```\n" + before3 + "\n```\n\n"
        "## AFTER (keyset seeks directly)\n\n```\n" + after3 + "\n```\n", encoding="utf-8")

    # ---- case 4: N+1 — count queries, not just latency
    prefix = str(tid)[:8]
    t0 = time.perf_counter()
    vehicles = c.execute(
        "SELECT id FROM vehicle WHERE tenant_id=%s LIMIT 200", (str(tid),)).fetchall()
    naive_q = 1
    for (vid,) in vehicles:
        c.execute("SELECT count(*) FROM alert WHERE tenant_id=%s AND vehicle_id=%s",
                  (str(tid), str(vid))).fetchone()
        naive_q += 1
    naive_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    c.execute(
        "SELECT v.id, count(a.id) FROM vehicle v LEFT JOIN alert a ON a.vehicle_id = v.id"
        " AND a.tenant_id = v.tenant_id WHERE v.tenant_id = %s GROUP BY v.id LIMIT 200",
        (str(tid),)).fetchall()
    join_ms = (time.perf_counter() - t0) * 1000
    (out / "case4-n-plus-1.md").write_text(
        "# Case 4: vehicle list with alert counts — ORM N+1 vs single join\n\n"
        f"200 vehicles (tenant `{prefix}…`).\n\n"
        f"## BEFORE (1 + N lazy queries): {naive_q} queries, {naive_ms:.0f} ms\n\n"
        "The deliberately naive ORM version lazy-loads one COUNT per vehicle.\n\n"
        f"## AFTER (single grouped join): 1 query, {join_ms:.0f} ms\n\n"
        "One `LEFT JOIN + GROUP BY` (or `selectinload`) returns all counts at once.\n",
        encoding="utf-8")
    print("wrote case2/3/4 evidence")


if __name__ == "__main__":
    main()
