"""Batch feature job: lake Parquet -> per-vehicle-day model features (DuckDB).

Same 4-feature semantics the processor scores on: dtc_count, harsh_count, idle_ratio,
km_since_service. The first three come from telemetry; km_since_service comes from
maintenance records when provided (else 0.0 — stated, not silently invented).
"""
from __future__ import annotations

from dataclasses import dataclass

import duckdb
from fleetsentinel.v1 import telemetry_pb2


@dataclass(frozen=True, slots=True)
class DayFeatures:
    vin: str
    day: str
    dtc_count: int
    harsh_count: int
    idle_ratio: float
    km_since_service: float

    def vector(self) -> list[float]:
        return [float(self.dtc_count), float(self.harsh_count), self.idle_ratio, self.km_since_service]


# Must match processor's HARSH_EVENTS (fs_processor.domain.bulk) — pinned by test, not import
# (batch must not depend on the processor package).
HARSH_EVENTS = (telemetry_pb2.HARSH_BRAKE, telemetry_pb2.HARSH_ACCEL, telemetry_pb2.OVERSPEED)


def build_day_features(
    con: duckdb.DuckDBPyConnection,
    lake_glob: str,
    service_km: dict[str, float] | None = None,
) -> list[DayFeatures]:
    """One row per (vehicle, UTC day). Pure SQL over Parquet; no cluster needed."""
    service_km = service_km or {}
    hb, ha, os = HARSH_EVENTS
    rows = con.execute(
        """
        SELECT vin,
               date_trunc('day', to_timestamp(ts_ms / 1000))::DATE AS day,
               count(*) AS n,
               sum(CASE WHEN len(dtc) > 0 THEN len(dtc) ELSE 0 END)::BIGINT AS dtc,
               sum(CASE WHEN evt IN (?, ?, ?) THEN 1 ELSE 0 END)::BIGINT AS harsh,
               sum(CASE WHEN ignition AND speed_kmh < 1.0 THEN 1 ELSE 0 END)::BIGINT AS idle_n,
               max(odo_km) AS odo_max,
        FROM read_parquet(?)
        GROUP BY vin, day
        ORDER BY vin, day
        """,
        [hb, ha, os, lake_glob],
    ).fetchall()
    out = []
    for vin, day, n, dtc, harsh, idle_n, _odo in rows:
        out.append(DayFeatures(
            vin=vin, day=str(day), dtc_count=int(dtc or 0), harsh_count=int(harsh or 0),
            idle_ratio=(float(idle_n) / n) if n else 0.0,
            km_since_service=float(service_km.get(vin, 0.0))))
    return out
