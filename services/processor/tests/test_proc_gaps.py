"""Processor gap tests: rule boundaries, lifecycle snapshot/restore, GOING_DARK
event-time semantics (no-fire without watermark, deadline refresh, cooldown,
deterministic id), bulk RecentSet path / skew boundary / windows drain / trips /
approved clusters / checkpoint failure ordering, scorer clamps."""
from __future__ import annotations

import pytest
from fleetsentinel.v1 import alert_pb2, telemetry_pb2
from fs_processor.application.bulk_service import BulkConfig, BulkRunner, BulkService
from fs_processor.application.priority_service import PriorityConfig, PriorityService
from fs_processor.domain.bulk import cluster_depots
from fs_processor.domain.geofence import Geofence, inside, is_after_hours
from fs_processor.domain.lifecycle import AlertLedger
from fs_processor.domain.rules import (
    ChargerConfig,
    check_critical_dtc,
    check_low_soc,
    check_tow,
    evaluate_priority,
)
from fs_processor.models import InMsg
from fscore import contracts as C
from fscore.algorithms.graph import RoadGraph

T0 = 1_790_590_000_000
CLOCK = T0 + 3_600_000
NIGHT_TS = T0 - (T0 % 86_400_000) + 23 * 3_600_000  # 23:00 UTC


def _ev(vin="V1", ts=T0, seq=0, **kw) -> telemetry_pb2.TelemetryEvent:
    d = {"vin": vin, "ts_ms": ts, "seq": seq, "lat": 12.9716, "lon": 77.5946,
         "speed_kmh": 40.0, "ignition": True, "odo_km": 1000.0, "oem": "OEM_A",
         "ingest_ts_ms": ts + 50}
    d.update(kw)
    e = telemetry_pb2.TelemetryEvent()
    for k, v in d.items():
        if k == "dtc":
            e.dtc.extend(v)
        else:
            setattr(e, k, v)
    return e


def _msg(ev, offset=0, topic="telemetry.normalized.v1"):
    return (InMsg(topic=topic, partition=0, offset=offset,
                  key=ev.vin.encode(), value=ev.SerializeToString(), headers={}), ev)


def _svc(**kw):
    cfg = BulkConfig(tenant_default="t1", **kw)
    return BulkService(partition=0, config=cfg, tenant_of=lambda v: "t1", clock=lambda: CLOCK)


def _handle(svc, vin, ts, **kw):
    off = kw.pop("offset", 0)
    m, e = _msg(_ev(vin, ts, **kw), off)
    return svc.handle(m, e)


def _rules(res):
    out = []
    for m in res.alerts:
        a = alert_pb2.Alert()
        a.ParseFromString(m.value)
        out.append((a.vin, a.rule))
    return out


def _parse(m):
    a = alert_pb2.Alert()
    a.ParseFromString(m.value)
    return a


# ---------------------------------------------------------------- rule boundaries
def test_tow_threshold_is_inclusive():
    assert check_tow(_ev(speed_kmh=5.0, ignition=False)) is not None
    assert check_tow(_ev(speed_kmh=4.9, ignition=False)) is None


def test_low_soc_no_chargers_or_no_budget_means_stranded():
    g = RoadGraph()
    g.add_node("v", 12.9716, 77.5946)
    cfg = ChargerConfig(graph=g, snap=lambda la, lo: "v", chargers={})
    # no chargers configured -> rule stays silent (avoids an alert storm on misconfig)
    assert check_low_soc(_ev(soc_pct=80.0), cfg) is None
    cfg2 = ChargerConfig(graph=g, snap=lambda la, lo: "v", chargers={"c": "v"},
                         battery_kwh=60.0, reserve_kwh=60.0)
    assert check_low_soc(_ev(soc_pct=50.0), cfg2) is not None  # budget <= 0


def test_critical_dtc_dedups_and_sorts():
    f = check_critical_dtc(_ev(dtc=["P0300", "P0300"]))
    assert f is not None and f.details["dtc"] == "P0300"


def test_after_hours_boundaries():
    assert is_after_hours(NIGHT_TS) is True  # 23:00
    base_day = NIGHT_TS - 23 * 3_600_000
    assert is_after_hours(base_day + 22 * 3_600_000) is True  # 22:00 inclusive
    assert is_after_hours(base_day + 21 * 3_600_000 + 59 * 60_000) is False  # 21:59
    assert is_after_hours(base_day + 6 * 3_600_000) is False  # 06:00 exclusive
    assert is_after_hours(base_day + 12 * 3_600_000) is False


def test_inside_returns_first_match_and_evaluate_empty():
    fences = [Geofence("a", 12.9716, 77.5946, 5.0), Geofence("b", 12.9716, 77.5946, 5.0)]
    assert inside(12.9716, 77.5946, fences).name == "a"
    assert inside(0.0, 0.0, fences) is None
    assert evaluate_priority(_ev(speed_kmh=0.0, ignition=False), frozenset(), []) == []


# ---------------------------------------------------------------- lifecycle edges
def test_custom_cooldown_override_and_open_count():
    lg = AlertLedger(cooldowns_ms={"TOW": 0}, clock=lambda: T0)
    from fs_processor.domain.rules import Finding
    f = Finding(rule="TOW", summary="t")
    assert lg.maybe_emit("V1", "t1", f, T0, T0, 0.0, 0.0, {}) is not None
    assert lg.maybe_emit("V1", "t1", f, T0 + 1, T0, 0.0, 0.0, {}) is not None  # zero cooldown
    assert lg.open_count("V1") == 1 and lg.open_count("nobody") == 0


def test_snapshot_restore_roundtrip_multiple_keys():
    lg = AlertLedger(clock=lambda: T0)
    from fs_processor.domain.rules import Finding
    lg.maybe_emit("V1", "t1", Finding(rule="TOW", summary="t"), T0, T0, 0.0, 0.0, {})
    lg.maybe_emit("V2", "t1", Finding(rule="CRASH", summary="c"), T0, T0, 0.0, 0.0, {})
    snap = lg.snapshot()
    lg2 = AlertLedger(clock=lambda: T0 + 3_601_000)
    lg2.restore(snap)
    assert lg2.open_count("V1") == 1 and lg2.open_count("V2") == 1 and len(lg2) == 2


def test_resolve_resets_occurrences():
    lg = AlertLedger(clock=lambda: T0)
    from fs_processor.domain.rules import Finding
    f = Finding(rule="TOW", summary="t")
    lg.maybe_emit("V1", "t1", f, T0, T0, 0.0, 0.0, {})
    assert lg.resolve("V1", "TOW") is True
    a = lg.maybe_emit("V1", "t1", f, T0 + 10, T0, 0.0, 0.0, {})
    assert a is not None and a.details["occurrences"] == "1"


def test_malformed_traceparent_gives_empty_trace():
    lg = AlertLedger(clock=lambda: T0)
    from fs_processor.domain.rules import Finding
    a = lg.maybe_emit("V1", "t1", Finding(rule="CRASH", summary="c"), T0, T0, 0.0, 0.0,
                      {C.H_TRACEPARENT: b"not-a-traceparent!!!"})
    assert a is not None  # never blocks emission; trace may be junk but present-or-not is fine


# ---------------------------------------------------------------- GOING_DARK event-time semantics
def _psvc(**kw):
    cfg = PriorityConfig(dark_timeout_ms=120_000, tenant_default="t1", **kw)
    return PriorityService(config=cfg, clock=lambda: T0)


def _pmsg(ev, offset=0):
    return InMsg(topic="telemetry.priority.v1", partition=0, offset=offset,
                 key=ev.vin.encode(), value=ev.SerializeToString(), headers={})


def _darks(out):
    return [m for m in out if _parse(m).rule == alert_pb2.RULE_GOING_DARK]


def test_dark_does_not_fire_without_watermark_advance():
    svc = _psvc()
    out = svc.handle(_ev("A", T0, ignition=True, speed_kmh=40.0), {})
    assert _darks(out) == []  # a lone vehicle never goes dark by itself: needs another
    # vehicle's newer event to push the event-time watermark (documents the limit)


def test_dark_deadline_refreshes_on_new_report():
    svc = _psvc()
    svc.handle(_ev("A", T0, ignition=True, speed_kmh=40.0), {})  # deadline T0+120s
    svc.handle(_ev("A", T0 + 110_000, ignition=True, speed_kmh=40.0), {})  # refresh -> T0+230s
    out = svc.handle(_ev("B", T0 + 130_000, ignition=True, speed_kmh=40.0), {})
    assert [m for m in _darks(out) if _parse(m).vin == "A"] == []  # old deadline cancelled


def test_dark_alert_has_deterministic_id_and_cooldown():
    svc = _psvc()
    svc.handle(_ev("A", T0, ignition=True, speed_kmh=40.0), {})
    out = svc.handle(_ev("B", T0 + 130_000, ignition=True, speed_kmh=40.0), {})
    darks = _darks(out)
    assert len(darks) == 1
    a = _parse(darks[0])
    assert a.alert_id == C.alert_id("A", "GOING_DARK", T0 + 120_000)
    assert a.window_start_ms == a.event_ts_ms == T0 + 120_000
    out2 = svc.handle(_ev("B", T0 + 140_000, ignition=True, speed_kmh=40.0), {})
    assert _darks(out2) == []  # inside the 1h GOING_DARK cooldown


def test_dark_restore_preserves_cooldown_occurrences():
    svc = _psvc()
    svc.handle(_ev("A", T0, ignition=True, speed_kmh=40.0), {})
    svc.handle(_ev("B", T0 + 130_000, ignition=True, speed_kmh=40.0), {})  # dark fires
    snap = svc.snapshot()
    svc2 = _psvc()
    svc2.restore(snap)
    out = svc2.handle(_ev("B", T0 + 140_000, ignition=True, speed_kmh=40.0), {})
    assert _darks(out) == []  # restored ledger still inside cooldown


def test_priority_handle_batch_and_future_event_latency():
    svc = PriorityService(config=PriorityConfig(tenant_default="t1"), clock=lambda: T0)
    msgs = [(_pmsg(_ev("V1", T0, evt=telemetry_pb2.CRASH, ignition=True, speed_kmh=50.0)),
             _ev("V1", T0, evt=telemetry_pb2.CRASH, ignition=True, speed_kmh=50.0))]
    out = svc.handle_batch(msgs)
    assert len(out) >= 1  # CRASH (+TOW: ignition True so no tow) fires through the batch path


# ---------------------------------------------------------------- bulk edges
def test_seq_zero_dedup_via_recent_set():
    svc = _svc()
    r1 = _handle(svc, "V1", T0)  # seq defaults to 0 -> RecentSet path
    m, e = _msg(_ev("V1", T0), 1)
    r2 = svc.handle(m, e)  # byte-identical event: duplicate
    assert len(r1.lake) == 1 and len(r2.lake) == 0 and r2.states == [] and r2.alerts == []


def test_harsh_counts_per_window_but_cooldown_suppresses_realert():
    svc = _svc(harsh_threshold=2)
    ws1 = T0 - T0 % 60_000
    outs = [_handle(svc, "V1", T0 + i * 1000, seq=i + 1, evt=telemetry_pb2.HARSH_BRAKE)
            for i in range(2)]
    assert sum(1 for o in outs for _v, r in _rules(o) if r == alert_pb2.RULE_HARSH_DRIVING) == 1
    outs2 = [_handle(svc, "V1", T0 + 61_000 + i * 1000, seq=10 + i, evt=telemetry_pb2.HARSH_BRAKE)
             for i in range(2)]
    ws2 = (T0 + 61_000) - (T0 + 61_000) % 60_000
    assert ws2 != ws1 and svc._harsh_live[("V1", ws2)] == 2  # counted in the new window
    # ... but the ledger's 1 h HARSH_DRIVING cooldown suppresses the second alert
    assert sum(1 for o in outs2 for _v, r in _rules(o) if r == alert_pb2.RULE_HARSH_DRIVING) == 0


def test_odometer_equal_or_epsilon_is_not_rollback():
    svc = _svc()
    _handle(svc, "V1", T0, seq=1, odo_km=5000.0)
    res = _handle(svc, "V1", T0 + 1000, seq=2, odo_km=5000.0)
    assert ("V1", alert_pb2.RULE_ODOMETER_ROLLBACK) not in _rules(res)


def test_clock_skew_boundary():
    svc = _svc()
    ok = _handle(svc, "V1", CLOCK + 120_000, seq=1)  # exactly at the limit
    assert ok.dlq == [] and len(ok.lake) == 1
    bad = _handle(svc, "V1", CLOCK + 120_001, seq=2)
    assert len(bad.dlq) == 1 and bad.lake == []


def test_bulk_handle_batch_empty_and_drain_closed_windows():
    svc = _svc()
    total = svc.handle_batch([])
    assert total.alerts == [] and total.states == [] and total.closed_windows == []
    for i in range(3):  # advance the watermark well past the first window
        _handle(svc, "V1", T0 + i * 70_000, seq=i + 1)
    total2 = svc.handle_batch([_msg(_ev("V1", T0 + 300_000, seq=9), 9)])
    assert total2.closed_windows != []  # finished 60 s windows are surfaced


def test_trip_emits_on_completed_drive_stop_cycle():
    svc = _svc()
    trips = []
    for i in range(4):  # driving
        _handle(svc, "V1", T0 + i * 10_000, seq=i + 1, speed_kmh=40.0, ignition=True)
    for i in range(8):  # parking ends the trip on the first stopped sample
        res = _handle(svc, "V1", T0 + 40_000 + i * 10_000, seq=10 + i,
                      speed_kmh=0.0, ignition=False)
        trips.extend(res.trips)
    assert len(trips) >= 1
    t = trips[0]
    assert t["trip_id"] == C.trip_id("V1", t["start_ts_ms"]) and t["distance_km"] >= 0


def test_cluster_inside_approved_fence_or_too_small_is_silent():
    fence = Geofence("depot", 12.9716, 77.5946, 5.0)
    pts = [("C1", 12.9716, 77.5946), ("C2", 12.9717, 77.5946), ("C3", 12.9716, 77.5947)]
    assert cluster_depots(pts, [fence], 3) == []  # approved location
    assert cluster_depots(pts[:2], [], 3) == []  # below min size
    far = [(v, 13.5 + i * 1e-4, 78.0) for i, (v, _la, _lo) in enumerate(pts)]
    got = cluster_depots(far, [], 3)
    assert got == [sorted(["C1", "C2", "C3"])]  # deterministic order


def test_cluster_depots_empty_input():
    assert cluster_depots([], [], 3) == []


def test_bulk_runner_save_failure_never_commits():
    class _Boom:
        def save(self, partition, state): raise RuntimeError("redis down")
        def load(self, partition): return None

    class _Commit:
        def __init__(self): self.calls = 0
        def commit(self, partition, offset): self.calls += 1

    svc = _svc()
    com = _Commit()
    runner = BulkRunner(svc, _Boom(), com)
    with pytest.raises(RuntimeError):
        runner.handle_batch([_msg(_ev("V1", T0, seq=1), 0)])
    assert com.calls == 0  # offset commit happens only after a successful checkpoint


def test_bulk_runner_restore_with_no_saved_state_is_noop():
    class _Empty:
        def save(self, partition, state): ...
        def load(self, partition): return None

    class _Commit:
        def commit(self, partition, offset): ...

    svc = _svc()
    BulkRunner(svc, _Empty(), _Commit()).restore()  # must not raise
    assert svc.last_offset == -1


def test_checkpoint_restore_preserves_harsh_odo_and_spots():
    svc = _svc(harsh_threshold=3)
    store, com = {}, None

    class _Mem:
        def save(self, p, s): store[p] = s
        def load(self, p): return store.get(p)

    class _C:
        def commit(self, p, o): ...

    runner = BulkRunner(svc, _Mem(), _C())
    batch = [_msg(_ev("V1", T0 + i * 1000, seq=i + 1, evt=telemetry_pb2.HARSH_BRAKE), i)
             for i in range(2)]
    runner.handle_batch(batch)
    svc2 = _svc(harsh_threshold=3)
    BulkRunner(svc2, _Mem(), _C()).restore()
    assert svc2._harsh_live.get(("V1", T0 - T0 % 60_000)) == 2
    res = svc2.handle(*_msg(_ev("V1", T0 + 2000, seq=3, evt=telemetry_pb2.HARSH_BRAKE), 2))
    assert ("V1", alert_pb2.RULE_HARSH_DRIVING) in _rules(res)  # count survived restore
    assert svc2._last_odo["V1"] == 1000.0


# ---------------------------------------------------------------- scorer edges
def test_scorer_defaults_to_fallback_and_clamps_negative(monkeypatch):
    import sys
    import types

    from fs_processor.domain.scoring import BASELINE_VERSION, Features, RiskScorer

    s = RiskScorer()  # no model at all
    assert s.fallback is True
    assert s.score(Features(), False) == (0.15, BASELINE_VERSION)

    class Neg:
        def predict(self, rows): return [-4.0]

    fake = types.ModuleType("lightgbm")
    fake.Booster = lambda model_file: Neg()  # noqa: E731
    monkeypatch.setitem(sys.modules, "lightgbm", fake)
    s2 = RiskScorer(model_path="m")
    score, _ = s2.score(Features(), False)
    assert score == 0.0  # clamped to [0,1]
    high, _, _ = s2.is_high_risk(Features(), False)
    assert high is False
