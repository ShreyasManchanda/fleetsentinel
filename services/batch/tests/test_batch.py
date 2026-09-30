"""Batch jobs over synthetic lake Parquet (no MinIO, no Postgres)."""
from __future__ import annotations

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fs_batch.jobs.features import HARSH_EVENTS, build_day_features
from fs_batch.jobs.recompute import authoritative_hourly, diff_against_streamed
from fs_batch.jobs.reconcile import FiredAlert, Incident, reconcile
from fs_batch.jobs.reports import carbon_by_fleet, fuel_idle_by_depot

T0 = 1_790_590_000_000


def _lake(path, rows: list[dict]) -> str:
    tbl = pa.Table.from_pylist(rows, schema=pa.schema([
        ("vin", pa.string()), ("ts_ms", pa.int64()), ("seq", pa.int64()),
        ("lat", pa.float64()), ("lon", pa.float64()), ("speed_kmh", pa.float32()),
        ("soc_pct", pa.float32()), ("fuel_pct", pa.float32()), ("odo_km", pa.float64()),
        ("dtc", pa.list_(pa.string())), ("evt", pa.int32()), ("ignition", pa.bool_()),
        ("sw_version", pa.string()), ("oem", pa.string()), ("ingest_ts_ms", pa.int64())]))
    p = str(path / "t.parquet")
    pq.write_table(tbl, p, compression="zstd")
    return p


def _row(vin, ts, **kw):
    d = {"vin": vin, "ts_ms": ts, "seq": 1, "lat": 12.97, "lon": 77.59, "speed_kmh": 40.0,
         "soc_pct": None, "fuel_pct": 60.0, "odo_km": 1000.0, "dtc": [], "evt": 1,
         "ignition": True, "sw_version": "1", "oem": "OEM_A", "ingest_ts_ms": ts}
    d.update(kw)
    return d


def test_harsh_constants_match_processor():
    from fs_processor.domain.bulk import HARSH_EVENTS as PROC_HARSH

    assert set(HARSH_EVENTS) == set(PROC_HARSH)


def test_day_features_counts(tmp_path):
    hb = int(HARSH_EVENTS[0])
    rows = [_row("V1", T0 + i * 1000, speed_kmh=0.0, dtc=(["P0300"] if i < 3 else []),
                 evt=(hb if i % 2 else 1)) for i in range(10)]
    feats = build_day_features(duckdb.connect(), _lake(tmp_path, rows))
    assert len(feats) == 1
    f = feats[0]
    assert (f.dtc_count, f.harsh_count) == (3, 5) and f.idle_ratio == pytest.approx(1.0)
    assert f.vector() == [3.0, 5.0, pytest.approx(1.0), 0.0]


def test_recompute_authoritative_and_diff(tmp_path):
    rows = [_row("V1", T0 + i * 60_000, odo_km=1000.0 + i) for i in range(5)]
    truth = authoritative_hourly(duckdb.connect(), _lake(tmp_path, rows))
    assert sum(w.n for w in truth) == 5
    assert diff_against_streamed(truth, truth) == []
    wrong = [truth[0].__class__(truth[0].vin, truth[0].hour, 999, truth[0].harsh, truth[0].dtc)]
    corr = diff_against_streamed(truth, wrong)
    assert len(corr) == 1 and corr[0].field == "n" and corr[0].authoritative == truth[0].n


def test_reports(tmp_path):
    rows = ([_row("V1", T0 + i * 1000, speed_kmh=0.0, odo_km=5000.0 + i * 0.01) for i in range(6)]
            + [_row("V2", T0 + i * 1000, speed_kmh=50.0, odo_km=100.0 + i, oem="OEM_B") for i in range(6)])
    glob = _lake(tmp_path, rows)
    depots = fuel_idle_by_depot(duckdb.connect(), glob)
    assert len(depots) == 1 and depots[0].vehicles == 1 and depots[0].idle_hours > 0
    carbon = {c.oem: c for c in carbon_by_fleet(duckdb.connect(), glob)}
    assert set(carbon) == {"OEM_A", "OEM_B"} and carbon["OEM_A"].co2_kg_estimate > 0


def test_reconcile_precision_recall_latency():
    inc = [Incident("TOW", "V1", 1000), Incident("CRASH", "V2", 2000), Incident("TOW", "V3", 3000)]
    alerts = [FiredAlert("V1", "TOW", 1100, 1500),  # matched, 500 ms latency
              FiredAlert("V1", "TOW", 1200, 1600),  # duplicate -> false positive
              FiredAlert("V9", "TOW", 2100, 2200)]  # wrong VIN -> false positive
    r = reconcile(inc, alerts)
    assert (r.matched, r.missed, r.false_positives) == (1, 2, 2)
    assert r.precision == pytest.approx(1 / 3) and r.recall == pytest.approx(1 / 3)
    assert r.p50_latency_ms == 500 and r.p95_latency_ms == 500
    assert reconcile([], []).recall == 0.0
