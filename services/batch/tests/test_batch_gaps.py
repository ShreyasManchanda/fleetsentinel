"""Batch gap tests: empty/odd lake inputs, service-km wiring, recompute
diff branches, reconcile edges, report filters. DuckDB + local Parquet only."""
from __future__ import annotations

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fs_batch.jobs.features import build_day_features
from fs_batch.jobs.recompute import authoritative_hourly, diff_against_streamed
from fs_batch.jobs.reconcile import MATCH_WINDOW_MS, FiredAlert, Incident, reconcile
from fs_batch.jobs.reports import carbon_by_fleet, fuel_idle_by_depot

T0 = 1_790_590_000_000

SCHEMA = pa.schema([
    ("vin", pa.string()), ("ts_ms", pa.int64()), ("seq", pa.int64()),
    ("lat", pa.float64()), ("lon", pa.float64()), ("speed_kmh", pa.float32()),
    ("soc_pct", pa.float32()), ("fuel_pct", pa.float32()), ("odo_km", pa.float64()),
    ("dtc", pa.list_(pa.string())), ("evt", pa.int32()), ("ignition", pa.bool_()),
    ("sw_version", pa.string()), ("oem", pa.string()), ("ingest_ts_ms", pa.int64())])


def _lake(path, rows: list[dict]) -> str:
    tbl = pa.Table.from_pylist(rows, schema=SCHEMA)
    p = str(path / "t.parquet")
    pq.write_table(tbl, p, compression="zstd")
    return p


def _row(vin, ts, **kw):
    d = {"vin": vin, "ts_ms": ts, "seq": 1, "lat": 12.97, "lon": 77.59, "speed_kmh": 40.0,
         "soc_pct": None, "fuel_pct": 60.0, "odo_km": 1000.0, "dtc": [], "evt": 1,
         "ignition": True, "sw_version": "1", "oem": "OEM_A", "ingest_ts_ms": ts}
    d.update(kw)
    return d


# ---------------------------------------------------------------- features
def test_day_features_empty_lake(tmp_path):
    assert build_day_features(duckdb.connect(), _lake(tmp_path, [])) == []


def test_day_features_service_km_wired_and_vector(tmp_path):
    rows = [_row("V1", T0 + i * 1000, speed_kmh=0.0) for i in range(4)]
    feats = build_day_features(duckdb.connect(), _lake(tmp_path, rows), service_km={"V1": 123.0})
    assert len(feats) == 1
    assert feats[0].km_since_service == 123.0
    assert feats[0].vector() == [0.0, 0.0, pytest.approx(1.0), 123.0]


def test_day_features_no_idle_rows(tmp_path):
    rows = [_row("V1", T0 + i * 1000, speed_kmh=50.0) for i in range(4)]
    feats = build_day_features(duckdb.connect(), _lake(tmp_path, rows))
    assert feats[0].idle_ratio == 0.0 and feats[0].dtc_count == 0


def test_day_features_groups_by_day(tmp_path):
    rows = [_row("V1", T0), _row("V1", T0 + 86_400_000)]
    feats = build_day_features(duckdb.connect(), _lake(tmp_path, rows))
    assert len(feats) == 2 and feats[0].day != feats[1].day


# ---------------------------------------------------------------- recompute
def test_recompute_empty_lake(tmp_path):
    assert authoritative_hourly(duckdb.connect(), _lake(tmp_path, [])) == []


def test_diff_reports_each_field_once(tmp_path):
    rows = [_row("V1", T0 + i * 60_000) for i in range(3)]
    truth = authoritative_hourly(duckdb.connect(), _lake(tmp_path, rows))
    assert diff_against_streamed(truth, [])[0].field == "missing_window"
    wrong = [truth[0].__class__(truth[0].vin, truth[0].hour, truth[0].n + 1,
                                truth[0].harsh + 1, truth[0].dtc + 1)]
    fields = sorted(c.field for c in diff_against_streamed(truth, wrong))
    assert fields == ["dtc", "harsh", "n"]
    for c in diff_against_streamed(truth, wrong):
        assert c.authoritative == getattr(truth[0], c.field)
        assert c.streamed == getattr(wrong[0], c.field)


def test_diff_ignores_streamed_windows_absent_from_truth(tmp_path):
    rows = [_row("V1", T0)]
    truth = authoritative_hourly(duckdb.connect(), _lake(tmp_path, rows))
    extra = list(truth) + [truth[0].__class__("V9", "2099-01-01 00", 5, 0, 0)]
    assert diff_against_streamed(truth, extra) == []


# ---------------------------------------------------------------- reconcile
def test_reconcile_empty_and_unknown_kind():
    r = reconcile([], [])
    assert (r.matched, r.missed, r.false_positives) == (0, 0, 0)
    assert (r.precision, r.recall) == (0.0, 0.0)
    assert r.p50_latency_ms is None and r.p95_latency_ms is None
    r = reconcile([Incident("ALIEN_KIND", "V1", 1000)], [FiredAlert("V1", "TOW", 1000, 1100)])
    assert (r.matched, r.missed) == (0, 1)  # unmapped kinds never match


def test_reconcile_window_boundary():
    inc = [Incident("TOW", "V1", 1000)]
    assert reconcile(inc, [FiredAlert("V1", "TOW", 1000 + MATCH_WINDOW_MS, 2000)]).matched == 1
    assert reconcile(inc, [FiredAlert("V1", "TOW", 1000 + MATCH_WINDOW_MS + 1, 2000)]).matched == 0


def test_reconcile_duplicate_alert_consumed_once():
    inc = [Incident("TOW", "V1", 1000)]
    alerts = [FiredAlert("V1", "TOW", 1100, 1500), FiredAlert("V1", "TOW", 1200, 1600)]
    r = reconcile(inc, alerts)
    assert (r.matched, r.missed, r.false_positives) == (1, 0, 1)


def test_reconcile_latency_clamped_and_percentiles():
    r = reconcile([Incident("TOW", "V1", 2000)], [FiredAlert("V1", "TOW", 1900, 1500)])
    assert r.p50_latency_ms == 0  # early detection clamps at zero, never negative
    # NOTE: single-VIN here; the multi-VIN same-index collision is a reported SRC bug, not pinned.
    inc = [Incident("TOW", "V1", 1000 * i) for i in range(5)]
    alerts = [FiredAlert("V1", "TOW", 1000 * i + 100, 1000 * i + (100 + 100 * i)) for i in range(5)]
    r = reconcile(inc, alerts)
    assert r.p50_latency_ms == 300 and r.p95_latency_ms == 500


def test_reconcile_breakdown_rule_mapping():
    inc = [Incident("BREAKDOWN", "V1", 1000)]
    assert reconcile(inc, [FiredAlert("V1", "CRITICAL_DTC", 1100, 1500)]).matched == 1
    assert reconcile(inc, [FiredAlert("V1", "MAINTENANCE_RISK", 1100, 1500)]).matched == 1
    assert reconcile(inc, [FiredAlert("V1", "TOW", 1100, 1500)]).matched == 0


# ---------------------------------------------------------------- reports
def test_reports_min_idle_filter_and_empty(tmp_path):
    rows = [_row("V1", T0 + i * 1000, speed_kmh=0.0) for i in range(6)]
    glob = _lake(tmp_path, rows)
    assert fuel_idle_by_depot(duckdb.connect(), glob, min_idle_min=60.0) == []  # ~seconds of idle filtered out
    assert carbon_by_fleet(duckdb.connect(), _lake(tmp_path, [])) == []


def test_carbon_uses_odometer_span(tmp_path):
    rows = [_row("V1", T0 + i * 1000, odo_km=100.0 + 2.0 * i) for i in range(6)]
    (fleet,) = carbon_by_fleet(duckdb.connect(), _lake(tmp_path, rows))
    assert fleet.km == pytest.approx(10.0)
    assert fleet.co2_kg_estimate == pytest.approx(10.0 * 0.08 * 2.68)
