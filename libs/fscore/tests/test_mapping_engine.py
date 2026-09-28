import json
import time
from pathlib import Path

import pytest
import yaml
from fscore import contracts
from fscore.mapping_engine import Mapping, MappingError, to_proto

ROOT = Path(__file__).resolve().parents[3]
MAPS = ROOT / "contracts" / "mappings"
NOW = 1_790_590_600_000  # just after the sample timestamps


def load(name: str) -> Mapping:
    return Mapping(yaml.safe_load((MAPS / name).read_text(encoding="utf-8")))


def sample(name: str) -> dict:
    return json.loads((MAPS / "samples" / name).read_text(encoding="utf-8"))


def test_oem_a_nested():
    r = load("oem_a.yaml").apply(sample("oem_a.json"), now_ms=NOW)
    assert not r.failed and len(r.ok) == 2 and r.batch_id == "a-000123"
    a = r.ok[0]
    assert a["vin"] == "1HGCM82633A004352" and a["lon"] == 77.5946 and a["speed_kmh"] == 64.2
    assert a["evt"] == "HARSH_BRAKE" and a["dtc"] == ["P0301"] and a["soc_pct"] == 41.0 and a["ignition"] is True
    assert a["sw_version"] == "4.2.1" and a["driver_ref"] == "drv_8f21" and a["oem"] == "OEM_A"
    assert r.ok[1]["evt"] == "POSITION" and r.ok[1]["dtc"] == []


def test_oem_b_units_and_csv():
    r = load("oem_b.yaml").apply(sample("oem_b.json"), now_ms=NOW)
    assert not r.failed
    a = r.ok[0]
    assert a["speed_kmh"] == pytest.approx(64.2, abs=0.1)          # mph -> km/h
    assert a["odo_km"] == pytest.approx(18234.2, abs=0.2)          # miles -> km
    assert a["dtc"] == ["P0301", "P0171"] and a["evt"] == "HARSH_BRAKE" and a["fuel_pct"] == 63.0
    assert a["ts_ms"] == 1790590502120 and a["ignition"] is True


def test_oem_c_brief_shape():
    r = load("oem_c.yaml").apply(sample("oem_c.json"), now_ms=NOW)
    assert not r.failed and r.ok[0]["seq"] == 88412 and r.ok[0]["lat"] == 21.1702


def test_oem_d_columnar_draft_and_batch_root_path():
    r = load("oem_d_draft.yaml").apply(sample("oem_d.json"), now_ms=NOW)
    assert not r.failed, r.failed
    a = r.ok[0]
    assert a["ts_ms"] == 1790590502000                             # epoch seconds -> ms
    assert a["soc_pct"] == pytest.approx(41.0) and a["soh_pct"] == pytest.approx(96.5)
    assert a["sw_version"] == "nimbus/5.2"                          # $$ = batch-level path
    assert a["evt"] == "HARSH_BRAKE" and r.ok[1]["evt"] == "POSITION"


def test_bad_records_go_to_failed_not_raise():
    m = load("oem_c.yaml")
    good = sample("oem_c.json")["events"][0]
    batch = {"batch_id": "x", "events": [
        good,
        {**good, "vin": "1HGCM82633A004353"},                       # bad check digit
        {**good, "ts": "2026-09-28T10:15:02.120Z", "lat": 123.0},   # lat out of range
        {**good, "ts": "2099-01-01T00:00:00Z"},                     # far future
        {k: v for k, v in good.items() if k != "lat"},              # missing required
        {**good, "soc_pct": 250},                                   # clamped, still valid
    ]}
    r = m.apply(batch, now_ms=NOW)
    reasons = [f.reason for f in r.failed]
    assert len(r.ok) == 2 and r.ok[1]["soc_pct"] == 100.0
    assert reasons.count(contracts.ERR_VALIDATION_FAILED) == 3 and reasons.count(contracts.ERR_CLOCK_SKEW) == 1


def test_bad_envelope_and_bad_json():
    m = load("oem_a.yaml")
    assert m.apply({"nope": 1}).failed[0].reason == contracts.ERR_BAD_ENVELOPE
    assert m.apply(b"{not json").failed[0].reason == contracts.ERR_BAD_ENVELOPE
    d = load("oem_d_draft.yaml")
    r = d.apply({"cols": ["vin"], "rows": [["a", "b"]], "bid": "z", "schema": "s"})
    assert r.failed[0].reason == contracts.ERR_BAD_ENVELOPE
    assert d.apply({"rows": [], "bid": "z"}).failed[0].reason == contracts.ERR_BAD_ENVELOPE   # missing columns


def test_bytes_and_str_payloads():
    m = load("oem_c.yaml")
    raw = (MAPS / "samples" / "oem_c.json").read_bytes()
    assert len(m.apply(raw, now_ms=NOW).ok) == 1 and len(m.apply(raw.decode(), now_ms=NOW).ok) == 1


@pytest.mark.parametrize("bad", [
    {"oem": "X"},                                                                       # no version/envelope
    {"oem": "X", "version": 1, "envelope": {"records": "$.a"}, "fields": {}},           # records not [*]
    {"oem": "X", "version": 1, "envelope": {"records": "$.a[*]"}, "fields": {"vin": {"path": "$.v"}}},  # required unmapped
])
def test_invalid_mappings_rejected(bad):
    with pytest.raises(MappingError):
        Mapping(bad)


def _doc(**field_overrides):
    base = yaml.safe_load((MAPS / "oem_c.yaml").read_text(encoding="utf-8"))
    base["fields"].update(field_overrides)
    return base


@pytest.mark.parametrize("override", [
    {"speed_kmh": {"path": "$.s", "transform": ["eval"]}},              # unknown transform
    {"speed_kmh": {"path": "$.s", "transform": [{"exec": "x"}]}},       # unsupported spec
    {"speed_kmh": {"path": "$..s"}},                                    # recursion not allowed
    {"speed_kmh": {"path": "$.a[*].s"}},                                # wildcard outside envelope
    {"nonsense": {"path": "$.n"}},                                      # unknown canonical field
    {"speed_kmh": {}},                                                  # no source
    {"evt": {"path": "$.e", "transform": [{"enum_map": {"X": "NOT_AN_EVENT"}}]}},
    {"speed_kmh": {"path": "not-a-path"}},
])
def test_mapping_safety_rules(override):
    with pytest.raises(MappingError):
        Mapping(_doc(**override))


def test_to_proto_roundtrip():
    from fleetsentinel.v1 import telemetry_pb2 as pb

    rec = load("oem_a.yaml").apply(sample("oem_a.json"), now_ms=NOW).ok[0]
    e = to_proto(rec, ingest_ts_ms=NOW, driver_ref_enc=b"cipher")
    e2 = pb.TelemetryEvent.FromString(e.SerializeToString())
    assert e2.vin == rec["vin"] and e2.evt == pb.HARSH_BRAKE and list(e2.dtc) == ["P0301"]
    assert e2.HasField("soc_pct") and not e2.HasField("fuel_pct") and e2.driver_ref_enc == b"cipher"


def test_all_registered_mappings_compile_and_have_samples():
    for f in ("oem_a", "oem_b", "oem_c", "oem_d_draft"):
        m = load(f"{f}.yaml")
        assert m.version >= 1


def test_throughput_single_core():
    m = load("oem_a.yaml")
    batch = sample("oem_a.json")
    batch["records"] = batch["records"] * 500                        # 1000 records
    m.apply(batch, now_ms=NOW)                                       # warm-up
    t0 = time.perf_counter()
    n = 0
    for _ in range(5):
        n += len(m.apply(batch, now_ms=NOW).ok)
    rate = n / (time.perf_counter() - t0)
    print(f"mapping engine: {rate:,.0f} records/s/core")
    # Baseline on a loaded dev laptop is ~11k rec/s/core. Stream B owns a batch-vectorised fast path (target >= 40k).
    assert rate > 8_000
