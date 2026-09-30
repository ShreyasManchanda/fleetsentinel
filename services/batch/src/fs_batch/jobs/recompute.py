"""Late-event recompute: the lake is authoritative; streaming windows are approximate.

Rebuilds per-(vin, hour) aggregates from Parquet and diffs them against a set of
streaming aggregates, returning corrections. Pure DuckDB + plain data.
"""
from __future__ import annotations

from dataclasses import dataclass

import duckdb


@dataclass(frozen=True, slots=True)
class WindowAgg:
    vin: str
    hour: str
    n: int
    harsh: int
    dtc: int


@dataclass(frozen=True, slots=True)
class Correction:
    vin: str
    hour: str
    field: str
    streamed: int
    authoritative: int


def authoritative_hourly(con: duckdb.DuckDBPyConnection, lake_glob: str) -> list[WindowAgg]:
    from fleetsentinel.v1 import telemetry_pb2

    hb, ha, os = (telemetry_pb2.HARSH_BRAKE, telemetry_pb2.HARSH_ACCEL, telemetry_pb2.OVERSPEED)
    rows = con.execute(
        """
        SELECT vin, date_trunc('hour', to_timestamp(ts_ms / 1000))::VARCHAR AS hour,
               count(*)::BIGINT AS n,
               sum(CASE WHEN evt IN (?, ?, ?) THEN 1 ELSE 0 END)::BIGINT AS harsh,
               sum(CASE WHEN len(dtc) > 0 THEN len(dtc) ELSE 0 END)::BIGINT AS dtc
        FROM read_parquet(?)
        GROUP BY vin, hour ORDER BY vin, hour
        """,
        [hb, ha, os, lake_glob],
    ).fetchall()
    return [WindowAgg(vin=v, hour=h, n=int(n), harsh=int(hh or 0), dtc=int(d or 0))
            for v, h, n, hh, d in rows]


def diff_against_streamed(truth: list[WindowAgg], streamed: list[WindowAgg]) -> list[Correction]:
    s = {(w.vin, w.hour): w for w in streamed}
    out: list[Correction] = []
    for w in truth:
        o = s.get((w.vin, w.hour))
        if o is None:
            out.append(Correction(w.vin, w.hour, "missing_window", 0, w.n))
            continue
        for field in ("n", "harsh", "dtc"):
            a, b = getattr(o, field), getattr(w, field)
            if a != b:
                out.append(Correction(w.vin, w.hour, field, a, b))
    return out
