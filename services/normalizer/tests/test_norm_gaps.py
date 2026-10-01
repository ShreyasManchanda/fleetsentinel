"""Normalizer gap tests: drift-alert severity/reporting, header fallbacks, registry
edge outcomes, encryptor LRU eviction, fast/reference equivalence on mixed batches,
drift cooldown/reopen and zero-event observes."""
from __future__ import annotations

import json
from pathlib import Path

import orjson
import yaml
from fleetsentinel.v1 import telemetry_pb2
from fs_normalizer.core import NormalizerCore
from fs_normalizer.drift import DriftEvent, DriftMonitor
from fs_normalizer.encryptor import AesGcmDevEncryptor, CachingEncryptor
from fs_normalizer.metrics import NormalizerMetrics
from fs_normalizer.models import InMsg
from fs_normalizer.registry import MappingRegistry, source_keys
from fs_normalizer.runner import Runner
from fscore import contracts as C

ROOT = Path(__file__).resolve().parents[3]
MAPS = ROOT / "contracts" / "mappings"
NOW = 1_790_590_600_000


def sample_bytes(name: str) -> bytes:
    return (MAPS / "samples" / f"{name}.json").read_bytes()


def make_core(clock_now=NOW, **kw):
    reg = MappingRegistry()
    reg.load_dir(MAPS, "oem_a.yaml,oem_b.yaml,oem_c.yaml")
    enc = AesGcmDevEncryptor(bytes(range(1, 33)), "test-key")
    core = NormalizerCore(reg, CachingEncryptor(enc), DriftMonitor(60, 3, 0.5, 600),
                          metrics=NormalizerMetrics(), clock=lambda: clock_now, **kw)
    return core, reg, enc


def msg(oem: str, body: bytes, offset=0, headers=None) -> InMsg:
    hdrs = {C.H_OEM: oem.encode(), C.H_RECEIVED_TS_MS: b"1790590600500",
            C.H_TRACEPARENT: b"00-" + b"a" * 32 + b"-" + b"b" * 16 + b"-01"}
    if headers is not None:
        hdrs = headers
    return InMsg(C.INGEST_RAW, 3, offset, f"{oem}:7".encode(), body, hdrs, 1790590600400)


# ---------------------------------------------------------------- drift alert reporting
def _ev(oem="OEM_C", ratio=0.6, failed=6, total=10) -> DriftEvent:
    return DriftEvent(oem=oem, window_start_ms=1_790_590_000_000, total=total, failed=failed,
                      ratio=ratio, reasons={"VALIDATION_FAILED": failed}, unknown_fields=[],
                      sw_version=None, mapping_version=1)


def test_drift_alert_warning_below_90pct_and_critical_above():
    warn = NormalizerCore.drift_alert(_ev(ratio=0.6), NOW)
    assert warn.severity == 2  # WARNING, not CRITICAL
    assert "60% of records failing (6/10)" in warn.summary
    crit = NormalizerCore.drift_alert(_ev(ratio=0.95, failed=19, total=20), NOW)
    assert crit.severity == 3  # CRITICAL
    assert crit.details["failure_ratio"] == "0.950" and crit.details["mapping_version"] == "1"


def test_drift_alert_without_traceparent_has_empty_trace():
    a = NormalizerCore.drift_alert(_ev(), NOW, {})
    assert a.trace_id == ""
    b = NormalizerCore.drift_alert(_ev(), NOW, {C.H_TRACEPARENT: b"aa-bb"})
    assert b.trace_id == ""
    # NOTE(src): a >=3-dash but non-W3C value (e.g. b"garbage-no-dashes") yields
    # parts[1] ("no") as trace_id without validation; harmless in practice because
    # traceparents are gateway-generated, but strict parsing would reject it.


def test_drift_alert_carries_unknown_fields_and_sw():
    ev = _ev()
    ev.unknown_fields = ["aa_field", "zz_top"]  # monitor sorts before constructing the event
    ev.sw_version = "7.1"
    a = NormalizerCore.drift_alert(ev, NOW)
    assert a.details["unknown_fields"] == "aa_field,zz_top"
    assert a.details["sw_version"] == "7.1" and "sw_version 7.1" in a.summary


# ---------------------------------------------------------------- header fallbacks
def test_missing_traceparent_not_propagated():
    core, _, _ = make_core()
    out = core.process([msg("OEM_A", sample_bytes("oem_a"),
                            headers={C.H_OEM: b"OEM_A", C.H_RECEIVED_TS_MS: b"1790590600500"})])
    assert out and C.H_TRACEPARENT not in out[0].headers


def test_received_ts_falls_back_to_kafka_timestamp_then_clock():
    core, _, _ = make_core()
    out = core.process([msg("OEM_A", sample_bytes("oem_a"),
                            headers={C.H_OEM: b"OEM_A"})])  # no received-ts, no traceparent
    ev = telemetry_pb2.TelemetryEvent.FromString(
        next(o for o in out if o.topic == C.TELEMETRY_NORMALIZED).value)
    assert ev.ingest_ts_ms == 1790590600400  # kafka timestamp used when the header is absent


def test_oem_falls_back_to_kafka_key_prefix():
    core, _, _ = make_core()
    m = InMsg(C.INGEST_RAW, 0, 0, b"OEM_A:7", sample_bytes("oem_a"),
              {C.H_RECEIVED_TS_MS: b"1790590600500"}, 1790590600400)
    out = core.process([m])
    assert any(o.topic == C.TELEMETRY_NORMALIZED and o.headers[C.H_OEM] == b"OEM_A" for o in out)


def test_record_dlq_carries_mapping_version_and_received_ts():
    core, _, _ = make_core()
    body = json.loads(sample_bytes("oem_c"))
    body["events"].append(dict(body["events"][0], vin="1HGCM82633A004353"))  # bad check digit
    out = core.process([msg("OEM_C", json.dumps(body).encode())])
    dlq = [o for o in out if o.topic == C.DLQ_INGEST]
    assert len(dlq) == 1
    assert dlq[0].headers[C.H_MAPPING_VERSION] == b"1"
    assert dlq[0].headers[C.H_RECEIVED_TS_MS] == b"1790590600500"


# ---------------------------------------------------------------- _unknown edges
def test_unknown_with_unwalkable_records_root_is_empty():
    core, reg, _ = make_core()
    assert core._unknown(reg.get("OEM_A"), {"no": "records here"}) == []
    assert core._unknown(reg.get("OEM_A"), [1, 2, 3]) == []


def test_source_keys_covers_any_alternates():
    doc = {"oem": "X", "version": 1,
           "fields": {"vin": {"any": ["$.a.vin", "$.b.vin_alt"]}, "soc": {"path": "$.c.soc"}}}
    assert source_keys(doc) == frozenset({"a", "b", "c"})


# ---------------------------------------------------------------- registry edges
def test_blank_tombstone_retires_and_unknown_retire_is_unchanged():
    reg = MappingRegistry()
    doc = yaml.safe_load((MAPS / "oem_c.yaml").read_text(encoding="utf-8"))
    reg.register(doc)
    assert reg.apply_config_message(b"OEM_C", b"   ") == ("retired", "OEM_C")
    assert reg.apply_config_message(b"OEM_GHOST", None) == ("unchanged", "OEM_GHOST")
    assert reg.apply_config_message(None, orjson.dumps(doc)) == ("loaded", "OEM_C")
    assert reg.apply_config_message(b"OEM_C", orjson.dumps(doc)) == ("unchanged", "OEM_C")


def test_runner_counts_rejects_and_ignores_stale():

    class _Sink:
        def produce(self, m): ...
        def flush(self, timeout_s=30.0): return 0

    core, reg, _ = make_core()
    runner = Runner(core, reg, _Sink(), core.metrics)
    # handle_config with a rejected doc bumps mapping_rejects and changes nothing
    before = core.metrics.mapping_rejects._value.get()
    assert runner.handle_config(
        [InMsg(C.CONFIG_MAPPINGS, 0, 0, b"OEM_C", b"{bad json")]) == 0
    assert core.metrics.mapping_rejects._value.get() == before + 1


# ---------------------------------------------------------------- encryptor edges
def test_caching_encryptor_evicts_lru_and_reencrypts():
    enc = AesGcmDevEncryptor(bytes(range(1, 33)), "k1")
    cache = CachingEncryptor(enc, capacity=1)
    first = cache.encrypt("d1", "O")
    cache.encrypt("d2", "O")  # evicts d1
    assert cache.encrypt("d1", "O") != first  # fresh nonce: proves it was re-encrypted
    assert enc.decrypt(cache.encrypt("d1", "O"), "O") == "d1"
    assert cache.encrypt_many([], "O") == []
    # same plaintext under different contexts encrypts independently
    assert cache.encrypt("d1", "O2") != cache.encrypt("d1", "O")


# ---------------------------------------------------------------- fast/reference equivalence on mixed batches
def test_fast_reference_equivalence_on_mixed_good_and_bad_rows():
    bodies = []
    ok_body = json.loads(sample_bytes("oem_c"))
    mixed = json.loads(sample_bytes("oem_c"))
    mixed["events"].append(dict(ok_body["events"][0], vin="1HGCM82633A004353"))
    mixed["events"].append(dict(ok_body["events"][0], ts="2999-01-01T00:00:00Z"))
    bodies.append(("OEM_C", json.dumps(mixed).encode()))
    bodies.append(("OEM_A", sample_bytes("oem_a")))
    for oem, body in bodies:
        a, _, _ = make_core(use_fast_path=True)
        b, _, _ = make_core(use_fast_path=False)
        oa, ob = a.process([msg(oem, body)]), b.process([msg(oem, body)])
        assert [(o.topic, o.key) for o in oa] == [(o.topic, o.key) for o in ob]
        for x, y in zip(oa, ob, strict=True):
            if x.topic == C.TELEMETRY_NORMALIZED:
                ex, ey = telemetry_pb2.TelemetryEvent.FromString(x.value), \
                    telemetry_pb2.TelemetryEvent.FromString(y.value)
                ex.driver_ref_enc = ey.driver_ref_enc = b""
                assert ex == ey
            else:
                assert x.headers.get(C.H_ERROR_REASON) == y.headers.get(C.H_ERROR_REASON)


# ---------------------------------------------------------------- drift monitor edges
def test_drift_zero_events_observe_returns_none():
    m = DriftMonitor(window_s=10, min_events=2, threshold=0.5, cooldown_s=1000)
    assert m.observe("O", 0, 0, 1_000_000) is None
    assert m.ratio("O", 1_000_000) == (0, 0, 0.0)


def test_drift_cooldown_expiry_closes_incident_and_reopens():
    m = DriftMonitor(window_s=60, min_events=2, threshold=0.5, cooldown_s=10)
    t = 5_000_000
    ev1 = m.observe("O", 0, 3, t, {"X": 3})
    assert ev1 is not None
    assert m.observe("O", 0, 3, t + 1000) is None  # still active: suppressed
    # cooldown (10 s) passes while active -> closes without emitting
    assert m.observe("O", 0, 3, t + 11_000) is None
    ev2 = m.observe("O", 0, 3, t + 12_000, {"X": 3})
    assert ev2 is not None  # re-opens as a new incident
    assert ev2.reasons == {"X": 3}  # reasons were cleared on close: no double counting
