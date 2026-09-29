"""PriorityService: per-event alerts, cooldown suppression, GOING_DARK in event time."""
from __future__ import annotations

from fleetsentinel.v1 import alert_pb2, telemetry_pb2
from fs_processor.application.priority_service import PriorityConfig, PriorityService
from fs_processor.models import InMsg
from fscore import contracts as C

T0 = 1_790_590_000_000


def _ev(vin, ts, **kw) -> telemetry_pb2.TelemetryEvent:
    d = {"vin": vin, "ts_ms": ts, "lat": 12.9716, "lon": 77.5946, "speed_kmh": 0.0,
         "ignition": False, "odo_km": 500.0, "oem": "OEM_A", "ingest_ts_ms": ts + 50}
    d.update(kw)
    e = telemetry_pb2.TelemetryEvent()
    for k, v in d.items():
        if k == "dtc":
            e.dtc.extend(v)
        else:
            setattr(e, k, v)
    return e


def _msg(ev, offset=0) -> InMsg:
    return InMsg(topic="telemetry.priority.v1", partition=0, offset=offset,
                 key=ev.vin.encode(), value=ev.SerializeToString(), headers={})


def _svc(**kw):
    cfg = PriorityConfig(dark_timeout_ms=120_000, tenant_default="t1", **kw)
    return PriorityService(config=cfg, clock=lambda: T0)


def _parse(m):
    a = alert_pb2.Alert()
    a.ParseFromString(m.value)
    return a


def test_tow_alert_emitted_with_deterministic_id():
    svc = _svc()
    out = svc.handle(_ev("V1", T0, speed_kmh=30.0), {})
    assert len(out) == 1 and out[0].topic == C.ALERTS
    a = alert_pb2.Alert()
    a.ParseFromString(out[0].value)
    assert a.alert_id == C.alert_id("V1", "TOW", T0)
    assert a.rule == alert_pb2.RULE_TOW and a.severity == alert_pb2.CRITICAL
    assert a.tenant_id == "t1" and a.event_ts_ms == T0


def test_repeat_inside_cooldown_suppressed():
    svc = _svc()
    assert len(svc.handle(_ev("V1", T0, speed_kmh=30.0), {})) == 1
    assert svc.handle(_ev("V1", T0 + 1000, speed_kmh=30.0), {}) == []


def test_going_dark_fires_on_event_time_watermark():
    svc = _svc()
    svc.handle(_ev("A", T0, ignition=True, speed_kmh=40.0), {})  # arms deadline T0+120s
    assert svc.handle(_ev("B", T0 + 30_000, ignition=True, speed_kmh=40.0), {}) == []
    out = svc.handle(_ev("B", T0 + 130_000, ignition=True, speed_kmh=40.0), {})
    darks = [m for m in out if _parse(m).rule == alert_pb2.RULE_GOING_DARK]
    assert len(darks) == 1
    a = _parse(darks[0])
    assert a.vin == "A" and a.severity == alert_pb2.WARNING


def test_restore_keeps_deadlines_armed():
    svc = _svc()
    svc.handle(_ev("A", T0, ignition=True, speed_kmh=40.0), {})
    snap = svc.snapshot()
    svc2 = _svc()
    svc2.restore(snap)
    out = svc2.handle(_ev("B", T0 + 130_000, ignition=True, speed_kmh=40.0), {})
    assert any(_parse(m).vin == "A" for m in out)


def test_priority_latency_bounded_by_event_time():
    svc = PriorityService(config=PriorityConfig(tenant_default="t1"), clock=lambda: T0 + 400)
    out = svc.handle(_ev("V1", T0, evt=telemetry_pb2.CRASH, ignition=True), {})
    assert len(out) == 1  # detection ~400ms after event time, well under the 5s NFR
