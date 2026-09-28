import json
from pathlib import Path

import orjson
import pytest
import yaml
from fleetsentinel.v1 import alert_pb2, telemetry_pb2
from fs_normalizer.core import SYSTEM_TENANT, NormalizerCore
from fs_normalizer.drift import DriftMonitor
from fs_normalizer.encryptor import AesGcmDevEncryptor, CachingEncryptor
from fs_normalizer.metrics import NormalizerMetrics
from fs_normalizer.models import InMsg, OutMsg
from fs_normalizer.registry import MappingRegistry, source_keys
from fs_normalizer.runner import DeliveryError, Runner
from fs_normalizer.settings import Settings
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
    core = NormalizerCore(reg, CachingEncryptor(enc), DriftMonitor(60, 3, 0.5, 600), metrics=NormalizerMetrics(),
                          clock=lambda: clock_now, **kw)
    return core, reg, enc


def msg(oem: str, body: bytes, offset=0, **hdr) -> InMsg:
    headers = {C.H_OEM: oem.encode(), C.H_RECEIVED_TS_MS: b"1790590600500", C.H_TRACEPARENT: b"00-" + b"a" * 32 + b"-" + b"b" * 16 + b"-01"}
    headers.update({k: v.encode() for k, v in hdr.items()})
    return InMsg(C.INGEST_RAW, 3, offset, f"{oem}:7".encode(), body, headers, 1790590600400)


class FakeSink:
    def __init__(self, undelivered=0):
        self.msgs: list[OutMsg] = []
        self.undelivered = undelivered

    def produce(self, m):
        self.msgs.append(m)

    def flush(self, timeout_s=30.0):
        return self.undelivered


def test_settings_lanes():
    s = Settings(lane="priority").resolved()
    assert (s.in_topic, s.out_topic, s.group_id) == ("ingest.priority.v1", "telemetry.priority.v1", "normalizer-priority")
    b = Settings().resolved()
    assert (b.in_topic, b.out_topic, b.group_id) == ("ingest.raw.v1", "telemetry.normalized.v1", "normalizer")


@pytest.mark.parametrize("oem,name,n", [("OEM_A", "oem_a", 2), ("OEM_B", "oem_b", 2), ("OEM_C", "oem_c", 1)])
def test_normalises_samples_to_protobuf_keyed_by_vin(oem, name, n):
    core, _, enc = make_core()
    out = core.process([msg(oem, sample_bytes(name))])
    events = [o for o in out if o.topic == C.TELEMETRY_NORMALIZED]
    assert len(events) == n and len(out) == n
    e = telemetry_pb2.TelemetryEvent.FromString(events[0].value)
    assert events[0].key == e.vin.encode() and e.vin == "1HGCM82633A004352" if oem != "OEM_B" else e.vin == "WVWHBUSR0JGF4CBF3"
    assert e.oem == oem and e.ingest_ts_ms == 1790590600500 and e.evt == telemetry_pb2.HARSH_BRAKE
    h = events[0].headers
    assert h[C.H_OEM] == oem.encode() and h[C.H_MAPPING_VERSION] == b"1" and h[C.H_TRACEPARENT].startswith(b"00-aaaa")
    assert (bool(e.driver_ref_enc) == (oem != "OEM_C")) and b"drv_8f21" not in e.driver_ref_enc and b"x9" not in e.driver_ref_enc
    ref = {"OEM_A": "drv_8f21", "OEM_B": "x9"}.get(oem)
    if ref:
        assert enc.decrypt(e.driver_ref_enc, oem) == ref


def test_fast_and_reference_paths_produce_identical_bytes_except_nonce():
    a, _, _ = make_core(use_fast_path=True)
    b, _, _ = make_core(use_fast_path=False)
    for name, oem in (("oem_a", "OEM_A"), ("oem_b", "OEM_B"), ("oem_c", "OEM_C")):
        oa = a.process([msg(oem, sample_bytes(name))])
        ob = b.process([msg(oem, sample_bytes(name))])
        for x, y in zip(oa, ob, strict=True):
            ex, ey = telemetry_pb2.TelemetryEvent.FromString(x.value), telemetry_pb2.TelemetryEvent.FromString(y.value)
            ex.driver_ref_enc = ey.driver_ref_enc = b""
            assert ex == ey and x.key == y.key


def test_unregistered_oem_goes_to_dlq_and_hot_reload_fixes_it():
    core, reg, _ = make_core()
    runner = Runner(core, reg, FakeSink(), core.metrics)
    body = sample_bytes("oem_d")
    out = core.process([msg("OEM_D", body)])
    assert len(out) == 1 and out[0].topic == C.DLQ_INGEST and out[0].key == b"OEM_D" and out[0].value == body
    assert out[0].headers[C.H_ERROR_REASON] == b"MAPPING_MISSING" and out[0].headers["dlq_kind"] == b"batch"
    doc = yaml.safe_load((MAPS / "oem_d_draft.yaml").read_text(encoding="utf-8"))
    assert runner.handle_config([InMsg(C.CONFIG_MAPPINGS, 0, 0, b"OEM_D", orjson.dumps(doc))]) == 1
    out2 = core.process([msg("OEM_D", body)])
    evs = [telemetry_pb2.TelemetryEvent.FromString(o.value) for o in out2 if o.topic == C.TELEMETRY_NORMALIZED]
    assert len(evs) == 2 and evs[0].soc_pct == pytest.approx(41.0) and evs[0].sw_version == "nimbus/5.2"
    # same version again -> unchanged; older -> stale; retire -> DLQ again
    assert runner.handle_config([InMsg(C.CONFIG_MAPPINGS, 0, 1, b"OEM_D", orjson.dumps(doc))]) == 0
    assert runner.handle_config([InMsg(C.CONFIG_MAPPINGS, 0, 2, b"OEM_D", None)]) == 1
    assert core.process([msg("OEM_D", body)])[0].topic == C.DLQ_INGEST


def test_config_message_outcomes():
    reg = MappingRegistry()
    doc = yaml.safe_load((MAPS / "oem_c.yaml").read_text(encoding="utf-8"))
    assert reg.apply_config_message(b"OEM_C", orjson.dumps(doc)) == ("loaded", "OEM_C")
    v2 = dict(doc, version=2)
    assert reg.apply_config_message(b"OEM_C", orjson.dumps(v2)) == ("loaded", "OEM_C") and reg.versions() == {"OEM_C": 2}
    assert reg.apply_config_message(b"OEM_C", orjson.dumps(doc)) == ("stale", "OEM_C")
    assert reg.apply_config_message(b"OEM_C", b"{not json")[0] == "rejected"
    assert reg.apply_config_message(b"OEM_C", b"[1]")[0] == "rejected"
    assert reg.apply_config_message(b"OEM_X", orjson.dumps({"oem": "OEM_X", "version": 1, "envelope": {}, "fields": {}}))[0] == "rejected"
    assert reg.apply_config_message(b"OEM_C", orjson.dumps({"oem": "OEM_C", "status": "retired"})) == ("retired", "OEM_C")
    assert reg.apply_config_message(b"OEM_C", None) == ("unchanged", "OEM_C") and reg.oems() == []


def test_record_failures_are_dlqd_with_reason_and_good_records_continue():
    core, _, _ = make_core()
    body = json.loads(sample_bytes("oem_c"))
    bad = dict(body["events"][0], vin="1HGCM82633A004353")            # bad check digit
    late = dict(body["events"][0], ts="2999-01-01T00:00:00Z")
    body["events"] += [bad, late]
    out = core.process([msg("OEM_C", json.dumps(body).encode())])
    dlq = [o for o in out if o.topic == C.DLQ_INGEST]
    assert sorted(o.headers[C.H_ERROR_REASON] for o in dlq) == [b"CLOCK_SKEW", b"VALIDATION_FAILED"]
    assert all(o.headers["dlq_kind"] == b"record" and json.loads(o.value)["vin"] for o in dlq)
    assert sum(o.topic == C.TELEMETRY_NORMALIZED for o in out) == 1
    assert core.process([msg("OEM_C", b"not json")])[0].headers[C.H_ERROR_REASON] == b"BAD_ENVELOPE"


def test_drift_alert_on_failure_spike_is_deterministic_and_single():
    core, reg, _ = make_core()
    body = orjson.dumps({"batch_id": "z", "events": [{"vin": "BAD", "ts": "2026-09-28T10:00:00Z", "lat": 1, "lon": 1}] * 4})
    alerts = []
    for i in range(6):
        out = core.process([msg("OEM_C", body, offset=i)])
        alerts += [o for o in out if o.topic == C.ALERTS]
    assert len(alerts) == 1                                            # one incident, not one alert per batch
    a = alert_pb2.Alert.FromString(alerts[0].value)
    assert a.rule == alert_pb2.RULE_SCHEMA_DRIFT and a.tenant_id == SYSTEM_TENANT and a.severity == alert_pb2.CRITICAL
    assert a.alert_id == C.alert_id("OEM_C", "SCHEMA_DRIFT", a.window_start_ms) and a.details["oem"] == "OEM_C"
    assert "VALIDATION_FAILED" in a.details["reasons"] and a.trace_id == "a" * 32 and a.detected_ts_ms == NOW
    core2, _, _ = make_core()
    a2 = [o for i in range(6) for o in core2.process([msg("OEM_C", body, offset=i)]) if o.topic == C.ALERTS]
    assert alert_pb2.Alert.FromString(a2[0].value).alert_id == a.alert_id
    assert core.metrics.records.labels("OEM_C", "failed")._value.get() == 24


def test_unmapped_oem_batches_raise_drift_and_carry_sw_version():
    core, _, _ = make_core()
    body = sample_bytes("oem_d")
    alerts = [o for i in range(5) for o in core.process([msg("OEM_D", body, offset=i)]) if o.topic == C.ALERTS]
    assert len(alerts) == 1
    a = alert_pb2.Alert.FromString(alerts[0].value)
    assert "MAPPING_MISSING" in a.details["reasons"] and a.details["sw_version"] == "nimbus/5.2"
    assert "nimbus/5.2" in a.summary and a.severity == alert_pb2.CRITICAL


def test_unknown_fields_detected_once_and_samples_are_clean():
    core, reg, _ = make_core()
    for name, oem in (("oem_a", "OEM_A"), ("oem_b", "OEM_B"), ("oem_c", "OEM_C")):
        assert core._unknown(reg.get(oem), json.loads(sample_bytes(name))) == []
    body = json.loads(sample_bytes("oem_b"))
    body["d"][0]["new_sensor"] = 5
    for i in range(2):
        core.process([msg("OEM_B", json.dumps(body).encode(), offset=i)])
    assert core.metrics.unknown_fields.labels("OEM_B")._value.get() == 2
    assert len(core._seen_unknown) == 1
    dcols = json.loads(sample_bytes("oem_d"))
    dcols["cols"][0] = "vin_new"
    doc = yaml.safe_load((MAPS / "oem_d_draft.yaml").read_text(encoding="utf-8"))
    reg.register(doc)
    assert core._unknown(reg.get("OEM_D"), dcols) == ["vin_new"]
    assert "vin" in source_keys(doc)


def test_drift_monitor_recovery_and_reopen():
    m = DriftMonitor(window_s=10, min_events=4, threshold=0.5, cooldown_s=1000)
    t = 1_000_000
    assert m.observe("O", 0, 3, t) is None                              # below min events
    ev = m.observe("O", 0, 2, t + 100, {"X": 5}, ["f"], "5.2", 1)
    assert ev is not None and ev.ratio == 1.0 and ev.unknown_fields == ["f"] and ev.sw_version == "5.2"
    assert m.observe("O", 0, 5, t + 200) is None                        # already active
    assert m.observe("O", 100, 0, t + 20_000) is None                   # window rolled, healthy -> closes
    ev2 = m.observe("O", 0, 50, t + 60_000)
    assert ev2 is not None and ev2.window_start_ms != ev.window_start_ms
    assert m.ratio("nobody", t) == (0, 0, 0.0)


def test_runner_commit_gate_and_batching():
    core, reg, _ = make_core()
    sink = FakeSink()
    r = Runner(core, reg, sink, core.metrics)
    assert r.handle_batch([]) == 0
    assert r.handle_batch([msg("OEM_A", sample_bytes("oem_a"))]) == 2 and len(sink.msgs) == 2
    bad = Runner(core, reg, FakeSink(undelivered=1), core.metrics)
    with pytest.raises(DeliveryError):
        bad.handle_batch([msg("OEM_A", sample_bytes("oem_a"))])


def test_aes_gcm_encryptor_properties():
    enc = AesGcmDevEncryptor(bytes(range(1, 33)), "k1")
    a, b = enc.encrypt("drv_1", "OEM_A"), enc.encrypt("drv_1", "OEM_A")
    assert a != b and enc.decrypt(a, "OEM_A") == "drv_1"
    with pytest.raises(Exception):
        enc.decrypt(a, "OEM_B")                                          # context is authenticated
    assert AesGcmDevEncryptor().is_fallback_key and not enc.is_fallback_key
    with pytest.raises(ValueError):
        AesGcmDevEncryptor(b"short")
    with pytest.raises(ValueError):
        AesGcmDevEncryptor(bytes(32), "")
    cache = CachingEncryptor(enc, capacity=2)
    x = cache.encrypt("d1", "O")
    assert cache.encrypt("d1", "O") == x
    many = cache.encrypt_many(["d1", "d2", "d3", "d2"], "O")            # capacity smaller than the batch
    assert many[1] == many[3] and len(many) == 4 and enc.decrypt(many[2], "O") == "d3"
    assert AesGcmDevEncryptor.from_b64("").is_fallback_key