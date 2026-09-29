"""BulkService: dedup, lateness, harsh/odo/idle rules, GPS plausibility, depot clusters,
checkpoint-restore replay determinism. Kafka-free (BulkRunner with fakes)."""
from __future__ import annotations

from fleetsentinel.v1 import alert_pb2, telemetry_pb2
from fs_processor.application.bulk_service import BulkConfig, BulkRunner, BulkService
from fs_processor.models import InMsg
from fscore import contracts as C

T0 = 1_790_590_000_000
CLOCK = T0 + 3_600_000


def _ev(vin, ts, seq=0, **kw) -> telemetry_pb2.TelemetryEvent:
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


def _msg(ev, offset=0):
    return (InMsg(topic="telemetry.normalized.v1", partition=0, offset=offset,
                  key=ev.vin.encode(), value=ev.SerializeToString(), headers={}), ev)


def _svc_handle(svc, vin, ts, **kw):
    off = kw.pop("offset", 0)
    msg, ev = _msg(_ev(vin, ts, **kw), off)
    return svc.handle(msg, ev)


def _svc(**kw):
    cfg = BulkConfig(tenant_default="t1", **kw)
    return BulkService(partition=0, config=cfg, tenant_of=lambda v: "t1",
                       clock=lambda: CLOCK)


def _rules(res):
    out = []
    for m in res.alerts:
        a = alert_pb2.Alert()
        a.ParseFromString(m.value)
        out.append((a.vin, a.rule))
    return out


def test_duplicate_seq_dropped_entirely():
    svc = _svc()
    m1, e1 = _msg(_ev("V1", T0, seq=7), 0)
    m2, e2 = _msg(_ev("V1", T0, seq=7), 1)
    r1 = svc.handle(m1, e1)
    r2 = svc.handle(m2, e2)
    assert len(r1.lake) == 1 and len(r1.states) == 1
    assert len(r2.lake) == 0 and len(r2.states) == 0 and r2.alerts == []


def test_future_event_goes_to_dlq():
    svc = _svc()
    m, e = _msg(_ev("V1", CLOCK + 121_000), 0)
    res = svc.handle(m, e)
    assert len(res.dlq) == 1 and res.dlq[0].headers[C.H_ERROR_REASON] == C.ERR_CLOCK_SKEW.encode()
    assert res.lake == [] and res.states == []


def test_late_event_counted_not_applied_but_still_in_lake():
    svc = _svc()
    for i in range(4):
        m, e = _msg(_ev("V1", T0 + i * 40_000, seq=i + 1), i)
        svc.handle(m, e)  # watermark -> ~T0+110s
    m, e = _msg(_ev("V1", T0 + 5_000, seq=99), 4)
    late = svc.handle(m, e)
    assert late.lake != [] and late.states != []  # forwarded + visible
    assert all("MAINTENANCE_RISK" != r for _, r in _rules(late))  # not applied to windows
    assert svc._windows.late_events == 1


def test_harsh_threshold_emits_once_per_window():
    svc = _svc(harsh_threshold=3)
    outs = [_svc_handle(svc, "V1", T0 + i * 1000, seq=i + 1, evt=telemetry_pb2.HARSH_BRAKE)
            for i in range(4)]
    rules = [r for o in outs for r in _rules(o)]
    assert rules.count(("V1", alert_pb2.RULE_HARSH_DRIVING)) == 1


def _svc_handle(svc, vin, ts, **kw):
    off = kw.pop("offset", 0)
    msg, ev = _msg(_ev(vin, ts, **kw), off)
    return svc.handle(msg, ev)


def test_odometer_rollback_alert():
    svc = _svc()
    _svc_handle(svc, "V1", T0, seq=1, odo_km=5000.0)
    res = _svc_handle(svc, "V1", T0 + 1000, seq=2, odo_km=4999.0)
    assert ("V1", alert_pb2.RULE_ODOMETER_ROLLBACK) in _rules(res)


def test_excessive_idling_with_dollar_cost():
    svc = _svc(idle_alert_s=60.0)
    for i in range(8):  # 70 s of ignition-on standstill, 10 s apart
        _svc_handle(svc, "V1", T0 + i * 10_000, seq=i + 1, speed_kmh=0.0,
                    ignition=True, odo_km=1000.0)
    # segment emits when the idle period ENDS (drive away)
    res = _svc_handle(svc, "V1", T0 + 80_000, seq=9, speed_kmh=40.0,
                      ignition=True, odo_km=1001.0)
    idle = [m for m in res.alerts if _parse(m).rule == alert_pb2.RULE_EXCESSIVE_IDLING]
    assert len(idle) == 1
    a = _parse(idle[0])
    assert float(a.details["cost_usd"]) > 0 and a.impact_usd > 0


def _parse(m):
    a = alert_pb2.Alert()
    a.ParseFromString(m.value)
    return a


def test_gps_jump_kept_out_of_state_but_counted():
    svc = _svc()
    _svc_handle(svc, "V1", T0, seq=1, lat=12.9716, lon=77.5946, speed_kmh=40.0)
    # 500 km in 60 s ≈ 30,000 km/h: impossible
    res = _svc_handle(svc, "V1", T0 + 60_000, seq=2, lat=17.0, lon=77.5946, speed_kmh=40.0)
    assert svc.gps_jumps == 1
    from fleetsentinel.v1 import state_pb2
    st = state_pb2.VehicleState()
    st.ParseFromString(res.states[-1].value)
    assert abs(st.lat - 12.9716) < 1e-9  # state holds last plausible position


def test_unapproved_depot_cluster_alert():
    svc = _svc(min_cluster=3)
    base = T0
    for i, vin in enumerate(("C1", "C2", "C3")):
        _svc_handle(svc, vin, base, seq=1, speed_kmh=0.0, ignition=True,
                    lat=12.9716 + i * 1e-4, lon=77.5946, odo_km=2000.0)
    res = _svc_handle(svc, "C1", base + 310_000, seq=2, speed_kmh=0.0, ignition=True,
                      lat=12.9716, lon=77.5946, odo_km=2000.0)
    assert ("C1", alert_pb2.RULE_UNAPPROVED_DEPOT_CLUSTER) in _rules(res)


def test_maintenance_risk_from_rule_baseline():
    svc = _svc()
    res = _svc_handle(svc, "V1", T0, seq=1, dtc=["P0300"])
    rules = _rules(res)
    assert ("V1", alert_pb2.RULE_MAINTENANCE_RISK) in rules
    res2 = _svc_handle(svc, "V2", T0, seq=1)
    assert ("V2", alert_pb2.RULE_MAINTENANCE_RISK) not in _rules(res2)


class _MemStore:
    def __init__(self):
        self.saved = {}
        self.order = []

    def save(self, partition, state):
        self.saved[partition] = state
        self.order.append("save")

    def load(self, partition):
        return self.saved.get(partition)


class _Committer:
    def __init__(self):
        self.order = []
        self.last = None

    def commit(self, partition, offset):
        self.order.append("commit")
        self.last = (partition, offset)


def test_checkpoint_before_commit_and_replay_identical_ids():
    svc = _svc()
    store, committer = _MemStore(), _Committer()
    runner = BulkRunner(svc, store, committer)
    batch = [(_msg(_ev("V1", T0, seq=1, dtc=["P0300"]), 0))]
    r1 = runner.handle_batch(batch)
    assert committer.last == (0, 0)
    assert store.order == ["save"] and committer.order == ["commit"]  # save first
    ids1 = sorted(_parse(m).alert_id for m in r1.alerts)
    assert ids1  # MAINTENANCE_RISK fired via the rule baseline

    svc2 = _svc()
    runner2 = BulkRunner(svc2, store, _Committer())
    runner2.restore()
    # Kill BEFORE the checkpoint was written: reprocessing the same batch on a fresh
    # instance must yield byte-identical alert ids (deterministic ids, no duplicates).
    svc3 = _svc()
    r3 = svc3.handle_batch(batch)
    ids3 = sorted(_parse(m).alert_id for m in r3.alerts)
    assert ids3 == ids1
    # Kill AFTER checkpoint+commit: the restored dedup state must suppress the re-feed
    # entirely (no duplicate alerts, no duplicate lake rows).
    r2 = runner2.service.handle_batch(batch)
    assert r2.alerts == [] and r2.lake == []
