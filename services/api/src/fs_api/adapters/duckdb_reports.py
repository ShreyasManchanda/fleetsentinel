"""DuckDB batch reports over the Parquet lake: PREDEFINED queries only (never free-form SQL), all parameters bound.
The lake carries no tenant column, so the query is restricted to the caller's VINs (a bound list from Postgres RLS)."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import duckdb

REPORTS: dict[str, dict[str, Any]] = {
    "fuel_idle_by_depot": {
        "columns": ["geohash5", "idle_hours", "vehicles"],
        "sql": """SELECT substr(geohash, 1, 5) AS geohash5, sum(idle_s) / 3600.0 AS idle_hours, count(DISTINCT vin) AS vehicles
                  FROM lake WHERE vin IN (SELECT unnest(?)) AND ts >= ? AND ts < CAST(? AS DATE) + 1 GROUP BY 1 ORDER BY idle_hours DESC LIMIT 200""",
    },
    "risk_by_sw_version": {
        "columns": ["sw_version", "vehicles", "dtc_per_1000km"],
        "sql": """SELECT sw_version, count(DISTINCT vin) AS vehicles, 1000.0 * sum(dtc_count) / nullif(max(odo_km) - min(odo_km), 0) AS dtc_per_1000km
                  FROM lake WHERE vin IN (SELECT unnest(?)) AND ts >= ? AND ts < CAST(? AS DATE) + 1 GROUP BY 1 ORDER BY 3 DESC NULLS LAST LIMIT 200""",
    },
    "utilization_by_hour": {
        "columns": ["hour", "moving_share"],
        "sql": """SELECT extract(hour FROM ts) AS hour, avg(CASE WHEN speed_kmh > 3 THEN 1.0 ELSE 0.0 END) AS moving_share
                  FROM lake WHERE vin IN (SELECT unnest(?)) AND ts >= ? AND ts < CAST(? AS DATE) + 1 GROUP BY 1 ORDER BY 1""",
    },
    "carbon_by_fleet": {
        "columns": ["oem", "km", "co2_kg_estimate"],
        "sql": """SELECT oem, sum(dist_km) AS km, sum(dist_km) * 0.12 AS co2_kg_estimate
                  FROM lake WHERE vin IN (SELECT unnest(?)) AND ts >= ? AND ts < CAST(? AS DATE) + 1 GROUP BY 1 ORDER BY 2 DESC LIMIT 200""",
    },
}


class DuckDbReportRunner:
    def __init__(self, lake_glob: str, timeout_s: float = 20.0):
        self._glob, self._timeout = lake_glob, timeout_s

    def _run(self, report_id: str, params: dict[str, Any], vins: list[str]) -> dict[str, Any]:
        spec = REPORTS[report_id]
        con = duckdb.connect(":memory:")
        try:
            con.execute("SET enable_external_access = true")
            glob = self._glob.replace("'", "''")  # trusted config value, never request input
            con.execute(f"CREATE VIEW lake AS SELECT * FROM read_parquet('{glob}', hive_partitioning = true)")
            t0 = time.perf_counter()
            date_from = params.get("from", "1970-01-01")
            date_to = params.get("to", "2100-01-01")
            rows = con.execute(spec["sql"], [vins, date_from, date_to]).fetchall()
            scanned = con.execute("SELECT count(*) FROM lake WHERE vin IN (SELECT unnest(?))", [vins]).fetchone()[0]
            return {"report_id": report_id, "rows_scanned": int(scanned), "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                    "columns": spec["columns"], "rows": [list(r) for r in rows]}
        finally:
            con.close()

    async def run(self, report_id: str, params: dict[str, Any], vins: list[str]) -> dict[str, Any]:
        return await asyncio.wait_for(asyncio.to_thread(self._run, report_id, params, vins), self._timeout)
