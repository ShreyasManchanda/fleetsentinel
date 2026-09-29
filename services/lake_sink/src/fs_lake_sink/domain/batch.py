"""Parquet batching: normalized TelemetryEvents -> zstd Parquet bytes. Pure (pyarrow only)."""
from __future__ import annotations

from io import BytesIO

import pyarrow as pa
import pyarrow.parquet as pq
from fleetsentinel.v1 import telemetry_pb2

SCHEMA = pa.schema([
    ("vin", pa.string()),
    ("ts_ms", pa.int64()),
    ("seq", pa.int64()),
    ("lat", pa.float64()),
    ("lon", pa.float64()),
    ("speed_kmh", pa.float32()),
    ("soc_pct", pa.float32()),
    ("fuel_pct", pa.float32()),
    ("odo_km", pa.float64()),
    ("dtc", pa.list_(pa.string())),
    ("evt", pa.int32()),
    ("ignition", pa.bool_()),
    ("sw_version", pa.string()),
    ("oem", pa.string()),
    ("ingest_ts_ms", pa.int64()),
])


def events_to_table(events: list[telemetry_pb2.TelemetryEvent]) -> pa.Table:
    return pa.Table.from_pylist([
        {
            "vin": e.vin, "ts_ms": e.ts_ms, "seq": e.seq, "lat": e.lat, "lon": e.lon,
            "speed_kmh": e.speed_kmh,
            "soc_pct": e.soc_pct if e.HasField("soc_pct") else None,
            "fuel_pct": e.fuel_pct if e.HasField("fuel_pct") else None,
            "odo_km": e.odo_km, "dtc": list(e.dtc), "evt": int(e.evt),
            "ignition": bool(e.ignition), "sw_version": e.sw_version, "oem": e.oem,
            "ingest_ts_ms": e.ingest_ts_ms,
        }
        for e in events
    ], schema=SCHEMA)


def table_to_parquet_bytes(table: pa.Table) -> bytes:
    buf = BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf.getvalue()


def events_to_parquet_bytes(events: list[telemetry_pb2.TelemetryEvent]) -> bytes:
    return table_to_parquet_bytes(events_to_table(events))


def parquet_bytes_to_table(raw: bytes) -> pa.Table:
    return pq.read_table(BytesIO(raw))
