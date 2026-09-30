"""Predefined batch reports over lake Parquet (never free-form SQL).

Mirrors the API's DuckDB report semantics (fuel_idle_by_depot, carbon_by_fleet) so the
batch layer and the serving layer cannot disagree on definitions.
"""
from __future__ import annotations

from dataclasses import dataclass

import duckdb


@dataclass(frozen=True, slots=True)
class DepotIdle:
    geohash5: str
    idle_hours: float
    vehicles: int


@dataclass(frozen=True, slots=True)
class FleetCarbon:
    oem: str
    km: float
    co2_kg_estimate: float


CO2_KG_PER_L_DIESEL = 2.68


def _gh5(con: duckdb.DuckDBPyConnection) -> None:
    con.create_function("gh5", lambda lat, lon: f"{lat:.1f}:{lon:.1f}", ["DOUBLE", "DOUBLE"], "VARCHAR")


def fuel_idle_by_depot(con: duckdb.DuckDBPyConnection, lake_glob: str, min_idle_min: float = 0.0) -> list[DepotIdle]:
    _gh5(con)
    rows = con.execute(
        """
        WITH idle AS (
          SELECT gh5(lat, lon) AS cell, vin,
                 count(*) / 3600.0 AS idle_h
          FROM read_parquet(?)
          WHERE ignition AND speed_kmh < 1.0
          GROUP BY cell, vin
        )
        SELECT cell, sum(idle_h), count(DISTINCT vin) FROM idle
        WHERE idle_h * 60 >= ?
        GROUP BY cell ORDER BY 2 DESC
        """,
        [lake_glob, min_idle_min],
    ).fetchall()
    return [DepotIdle(geohash5=c, idle_hours=float(h), vehicles=int(v)) for c, h, v in rows]


def carbon_by_fleet(con: duckdb.DuckDBPyConnection, lake_glob: str,
                    litres_per_km: float = 0.08) -> list[FleetCarbon]:
    """Distance from odometer deltas per vehicle (robust to sampling gaps)."""
    rows = con.execute(
        """
        WITH span AS (
          SELECT vin, oem, max(odo_km) - min(odo_km) AS km
          FROM read_parquet(?)
          GROUP BY vin, oem
        )
        SELECT oem, sum(km) FROM span GROUP BY oem ORDER BY oem
        """,
        [lake_glob],
    ).fetchall()
    return [FleetCarbon(oem=o, km=float(k or 0.0), co2_kg_estimate=float(k or 0.0) * litres_per_km * CO2_KG_PER_L_DIESEL)
            for o, k in rows]
