"""Ground-truth oracle: every injected incident + per-VIN sent-sequence ledger, written as Parquet."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from fscore.contracts import NAMESPACE_FS

# incident kinds
TOW, CRASH, GOING_DARK, DEPOT_CLUSTER, BREAKDOWN = "TOW", "CRASH", "GOING_DARK", "DEPOT_CLUSTER", "BREAKDOWN"
GPS_JUMP, STUCK_SPEED, ODOMETER_ROLLBACK, CLOCK_SKEW, BURST = "GPS_JUMP", "STUCK_SPEED", "ODOMETER_ROLLBACK", "CLOCK_SKEW", "BURST"
SCHEMA_DRIFT = "SCHEMA_DRIFT"

GT_SCHEMA = pa.schema([
    ("incident_id", pa.string()), ("kind", pa.string()), ("vin", pa.string()),
    ("start_ts_ms", pa.int64()), ("end_ts_ms", pa.int64()), ("lat", pa.float64()), ("lon", pa.float64()),
    ("value", pa.float64()), ("group_id", pa.string()), ("note", pa.string()),
])


def _bc(x, n: int, dtype) -> np.ndarray:
    if x is None:
        return np.full(n, np.nan) if dtype is float else np.zeros(n, dtype=dtype)
    return np.broadcast_to(np.asarray(x, dtype=dtype), (n,)).copy()


class GroundTruth:
    """Collects incidents as columnar chunks (vectorised appends); vin lookups use the global fleet vin array."""

    def __init__(self, vins: np.ndarray):
        self.vins = vins
        self._chunks: list[dict[str, Any]] = []

    def add(self, kind: str, vin_idx, start_ms, end_ms=None, lat=None, lon=None, value=None, group_id: str = "", note: str = "") -> None:
        vin_idx = np.atleast_1d(np.asarray(vin_idx, dtype=np.int64))
        n = len(vin_idx)
        if n == 0:
            return
        start = _bc(start_ms, n, np.int64)
        self._chunks.append({
            "kind": kind, "vin_idx": vin_idx, "start": start, "end": start.copy() if end_ms is None else _bc(end_ms, n, np.int64),
            "lat": _bc(lat, n, float), "lon": _bc(lon, n, float), "value": _bc(value, n, float),
            "group": group_id, "note": note,
        })

    def add_global(self, kind: str, start_ms: int, end_ms: int, value: float = float("nan"), note: str = "") -> None:
        """Fleet-wide incident (burst window, schema drift): vin is recorded as *."""
        self.add(kind, [-1], start_ms, end_ms, value=value, note=note)

    def count(self, kind: str | None = None) -> int:
        return sum(len(c["vin_idx"]) for c in self._chunks if kind is None or c["kind"] == kind)

    def merge(self, other: GroundTruth) -> None:
        self._chunks.extend(other._chunks)

    def table(self) -> pa.Table:
        if not self._chunks:
            return GT_SCHEMA.empty_table()
        ch = self._chunks
        kinds = [k for c in ch for k in [c["kind"]] * len(c["vin_idx"])]
        groups = [g for c in ch for g in [c["group"]] * len(c["vin_idx"])]
        notes = [g for c in ch for g in [c["note"]] * len(c["vin_idx"])]
        vidx = np.concatenate([c["vin_idx"] for c in ch])
        vin = np.where(vidx < 0, "*", self.vins[np.maximum(vidx, 0)]).tolist()
        start = np.concatenate([c["start"] for c in ch])
        ids = [str(uuid.uuid5(NAMESPACE_FS, f"gt|{k}|{v}|{s}")) for k, v, s in zip(kinds, vin, start.tolist(), strict=True)]
        cat = {key: np.concatenate([c[key] for c in ch]) for key in ("end", "lat", "lon", "value")}
        return pa.table({
            "incident_id": ids, "kind": kinds, "vin": vin, "start_ts_ms": start, "end_ts_ms": cat["end"],
            "lat": cat["lat"], "lon": cat["lon"], "value": cat["value"], "group_id": groups, "note": notes,
        }, schema=GT_SCHEMA)

    def write(self, path: Path) -> int:
        t = self.table()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(t, path, compression="zstd")
        return t.num_rows


class SeqLedger:
    """Per-VIN sent-sequence ledger for the reconciliation report (sent counts per lane, duplicates, out-of-order)."""

    def __init__(self, vins: np.ndarray):
        n = len(vins)
        self.vins = vins
        self.first_seq = np.full(n, np.iinfo(np.int64).max, dtype=np.int64)
        self.last_seq = np.full(n, -1, dtype=np.int64)
        self.unique = np.zeros(n, dtype=np.int64)
        self.dups = np.zeros(n, dtype=np.int64)
        self.late = np.zeros(n, dtype=np.int64)
        self.bulk = np.zeros(n, dtype=np.int64)
        self.event = np.zeros(n, dtype=np.int64)

    def record(self, vin_idx: np.ndarray, seq: np.ndarray, is_dup: np.ndarray, is_late: np.ndarray, is_event: np.ndarray) -> None:
        fresh = ~is_dup
        np.add.at(self.unique, vin_idx[fresh], 1)
        np.add.at(self.dups, vin_idx[is_dup], 1)
        np.add.at(self.late, vin_idx[is_late], 1)
        np.add.at(self.event, vin_idx[is_event], 1)
        np.add.at(self.bulk, vin_idx[~is_event], 1)
        f_idx, f_seq = vin_idx[fresh], seq[fresh]
        np.minimum.at(self.first_seq, f_idx, f_seq)
        np.maximum.at(self.last_seq, f_idx, f_seq)

    def table(self, oem: np.ndarray | None = None) -> pa.Table:
        cols: dict[str, Any] = {
            "vin": self.vins.tolist(), "first_seq": np.where(self.first_seq == np.iinfo(np.int64).max, -1, self.first_seq), "last_seq": self.last_seq,
            "sent_unique": self.unique, "sent_duplicate_records": self.dups, "sent_out_of_order": self.late,
            "sent_bulk_lane": self.bulk, "sent_event_lane": self.event,
        }
        if oem is not None:
            cols["oem"] = oem.tolist()
        return pa.table(cols)

    def write(self, path: Path, oem: np.ndarray | None = None) -> int:
        t = self.table(oem)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(t, path, compression="zstd")
        return t.num_rows
