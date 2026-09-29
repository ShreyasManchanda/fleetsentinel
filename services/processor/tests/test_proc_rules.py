"""Priority-lane rule unit tests (pure domain)."""
from __future__ import annotations

from fleetsentinel.v1 import alert_pb2, telemetry_pb2
from fs_processor.domain.geofence import Geofence
from fs_processor.domain.rules import (
    ChargerConfig,
    check_crash,
    check_critical_dtc,
    check_geofence,
    check_low_soc,
    check_tow,
    evaluate_priority,
)
from fscore.algorithms.graph import RoadGraph

BASE_TS = 1_790_590_000_000  # ~2026-09-28 10am UTC


def _ev(**kw) -> telemetry_pb2.TelemetryEvent:
    d = {"vin": "1HGCM82633A004352", "ts_ms": BASE_TS, "lat": 12.9716, "lon": 77.5946,
         "speed_kmh": 0.0, "ignition": False, "odo_km": 1000.0, "oem": "OEM_A",
         "ingest_ts_ms": BASE_TS + 100}
    d.update(kw)
    e = telemetry_pb2.TelemetryEvent()
    for k, v in d.items():
        if k == "dtc":
            e.dtc.extend(v)
        else:
            setattr(e, k, v)
    return e


NIGHT_TS = 1_790_590_000_000 - (1_790_590_000_000 % 86_400_000) + 23 * 3_600_000  # 23:00 UTC


def test_tow_movement_ignition_off():
    f = check_tow(_ev(speed_kmh=25.0, ignition=False))
    assert f is not None and f.rule == "TOW"
    assert check_tow(_ev(speed_kmh=25.0, ignition=True)) is None  # driving, not towed
    assert check_tow(_ev(speed_kmh=2.0, ignition=False)) is None  # below threshold
    assert check_tow(_ev(evt=telemetry_pb2.TOW, speed_kmh=0.0, ignition=False)) is not None


def test_crash_only_on_crash_event():
    assert check_crash(_ev(evt=telemetry_pb2.CRASH)) is not None
    assert check_crash(_ev(evt=telemetry_pb2.HARSH_BRAKE, speed_kmh=80.0)) is None


def test_critical_dtc_matches_severity_list():
    f = check_critical_dtc(_ev(dtc=["P0301", "P0300"]))
    assert f is not None and "P0300" in f.summary and "P0301" not in f.details["dtc"]
    assert check_critical_dtc(_ev(dtc=["P0301"])) is None
    assert check_critical_dtc(_ev()) is None


def test_geofence_watchlist_after_hours_only():
    fence = Geofence("depot", 12.9716, 77.5946, 0.5)
    wl = frozenset({"1HGCM82633A004352"})
    far = {"lat": 13.5, "lon": 78.0}
    assert check_geofence(_ev(ts_ms=NIGHT_TS, **far), wl, [fence]) is not None
    assert check_geofence(_ev(ts_ms=NIGHT_TS, **far), frozenset(), [fence]) is None  # not watchlisted
    assert check_geofence(_ev(ts_ms=NIGHT_TS), wl, [fence]) is None  # inside fence
    day_ts = NIGHT_TS - 12 * 3_600_000  # 11:00 UTC
    assert check_geofence(_ev(ts_ms=day_ts, **far), wl, [fence]) is None  # daytime


def _charger_cfg() -> ChargerConfig:
    g = RoadGraph()
    g.add_node("v", 12.9716, 77.5946)
    g.add_node("c", 12.9726, 77.5956)
    g.add_edge("v", "c", 0.2, 0.05)
    return ChargerConfig(graph=g, snap=lambda la, lo: "v", chargers={"chg1": "c"},
                         battery_kwh=60.0, reserve_kwh=2.0)


def test_low_soc_strandingneeds_unreachable_charger():
    cfg = _charger_cfg()
    e = _ev(soc_pct=1.0)
    f = check_low_soc(e, cfg)  # budget = 0.6-2 <0 -> unreachable
    assert f is not None and f.rule == "LOW_SOC_STRANDING"
    assert check_low_soc(_ev(soc_pct=80.0), cfg) is None  # easily reachable
    assert check_low_soc(_ev(), cfg) is None  # no SoC field (ICE)
    assert check_low_soc(_ev(soc_pct=1.0), None) is None  # no config


def test_evaluate_priority_stable_order_and_severity():
    ev = _ev(evt=telemetry_pb2.CRASH, speed_kmh=30.0, ignition=False, dtc=["P0300"])
    out = evaluate_priority(ev, frozenset(), [])
    assert [f.rule for f in out] == ["TOW", "CRASH", "CRITICAL_DTC"]
    from fs_processor.domain.rules import RULE_TO_ENUM, RULE_TO_SEVERITY
    assert RULE_TO_SEVERITY["TOW"] == alert_pb2.CRITICAL
    assert RULE_TO_ENUM["TOW"] == alert_pb2.RULE_TOW
