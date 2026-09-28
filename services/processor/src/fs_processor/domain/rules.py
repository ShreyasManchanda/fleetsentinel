"""Stateless priority-lane rules. Each rule is a pure function of one TelemetryEvent
(plus injected config): given an event it returns a finding or None. No clocks, no I/O."""
from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass, field

from fleetsentinel.v1 import alert_pb2, telemetry_pb2
from fscore.algorithms.graph import RoadGraph, nearest_reachable
from fscore.dtc import CRITICAL_DTCS

from .geofence import Geofence, watchlist_breach

TOW_SPEED_KMH = 5.0

RULE_TO_SEVERITY = {
    "TOW": alert_pb2.CRITICAL,
    "CRASH": alert_pb2.CRITICAL,
    "CRITICAL_DTC": alert_pb2.CRITICAL,
    "GEOFENCE_BREACH": alert_pb2.WARNING,
    "LOW_SOC_STRANDING": alert_pb2.WARNING,
    "GOING_DARK": alert_pb2.WARNING,
    "EXCESSIVE_IDLING": alert_pb2.WARNING,
    "HARSH_DRIVING": alert_pb2.WARNING,
    "MAINTENANCE_RISK": alert_pb2.WARNING,
    "UNAPPROVED_DEPOT_CLUSTER": alert_pb2.WARNING,
    "ODOMETER_ROLLBACK": alert_pb2.WARNING,
}

RULE_TO_ENUM = {
    "TOW": alert_pb2.RULE_TOW,
    "CRASH": alert_pb2.RULE_CRASH,
    "CRITICAL_DTC": alert_pb2.RULE_CRITICAL_DTC,
    "GEOFENCE_BREACH": alert_pb2.RULE_GEOFENCE_BREACH,
    "LOW_SOC_STRANDING": alert_pb2.RULE_LOW_SOC_STRANDING,
    "GOING_DARK": alert_pb2.RULE_GOING_DARK,
    "EXCESSIVE_IDLING": alert_pb2.RULE_EXCESSIVE_IDLING,
    "HARSH_DRIVING": alert_pb2.RULE_HARSH_DRIVING,
    "MAINTENANCE_RISK": alert_pb2.RULE_MAINTENANCE_RISK,
    "UNAPPROVED_DEPOT_CLUSTER": alert_pb2.RULE_UNAPPROVED_DEPOT_CLUSTER,
    "ODOMETER_ROLLBACK": alert_pb2.RULE_ODOMETER_ROLLBACK,
}


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    summary: str
    details: dict[str, str] = field(default_factory=dict)
    impact_usd: float = 0.0


def check_tow(ev: telemetry_pb2.TelemetryEvent) -> Finding | None:
    """Movement while ignition is off, or an OEM-flagged TOW event."""
    if ev.evt == telemetry_pb2.TOW or (not ev.ignition and ev.speed_kmh >= TOW_SPEED_KMH):
        return Finding(
            rule="TOW",
            summary=f"Possible tow/theft: {ev.vin} moving at {ev.speed_kmh:.0f} km/h with ignition off",
            details={"speed_kmh": f"{ev.speed_kmh:.1f}", "ignition": str(bool(ev.ignition))},
        )
    return None


def check_crash(ev: telemetry_pb2.TelemetryEvent) -> Finding | None:
    if ev.evt == telemetry_pb2.CRASH:
        return Finding(
            rule="CRASH", summary=f"Crash reported for {ev.vin}",
            details={"speed_kmh": f"{ev.speed_kmh:.1f}"},
        )
    return None


def check_critical_dtc(ev: telemetry_pb2.TelemetryEvent) -> Finding | None:
    hit = sorted({c for c in ev.dtc if c in CRITICAL_DTCS})
    if hit:
        return Finding(
            rule="CRITICAL_DTC", summary=f"Critical DTC(s) {','.join(hit)} on {ev.vin}",
            details={"dtc": ",".join(hit)},
        )
    return None


def check_geofence(
    ev: telemetry_pb2.TelemetryEvent,
    watchlist: frozenset[str],
    approved: list[Geofence],
) -> Finding | None:
    if watchlist_breach(ev.vin, ev.lat, ev.lon, ev.ts_ms, watchlist, approved):
        return Finding(
            rule="GEOFENCE_BREACH",
            summary=f"Watchlisted {ev.vin} outside approved areas after hours",
            details={"lat": f"{ev.lat:.5f}", "lon": f"{ev.lon:.5f}"},
        )
    return None


@dataclass(frozen=True, slots=True)
class ChargerConfig:
    graph: RoadGraph
    snap: Callable[[float, float], Hashable]
    chargers: Mapping[str, Hashable]  # charger id -> graph node
    battery_kwh: float = 60.0
    reserve_kwh: float = 2.0


def check_low_soc(ev: telemetry_pb2.TelemetryEvent, cfg: ChargerConfig | None) -> Finding | None:
    """LOW_SOC_STRANDING: SoC below the energy needed for the nearest reachable charger (A*)."""
    if cfg is None or not ev.HasField("soc_pct") or not cfg.chargers:
        return None
    budget = ev.soc_pct / 100.0 * cfg.battery_kwh - cfg.reserve_kwh
    start = cfg.snap(ev.lat, ev.lon)
    res = nearest_reachable(cfg.graph, start, dict(cfg.chargers), budget) if budget > 0 else None
    if res is not None and res.found:
        return None
    return Finding(
        rule="LOW_SOC_STRANDING",
        summary=f"{ev.vin} at {ev.soc_pct:.0f}% SoC cannot reach a charger",
        details={"soc_pct": f"{ev.soc_pct:.1f}", "budget_kwh": f"{max(budget, 0.0):.2f}"},
    )


def evaluate_priority(
    ev: telemetry_pb2.TelemetryEvent,
    watchlist: frozenset[str],
    approved: list[Geofence],
    charger_cfg: ChargerConfig | None = None,
) -> list[Finding]:
    """All stateless priority findings for one event, in stable rule order."""
    out: list[Finding] = []
    for f in (
        check_tow(ev),
        check_crash(ev),
        check_critical_dtc(ev),
        check_geofence(ev, watchlist, approved),
        check_low_soc(ev, charger_cfg),
    ):
        if f is not None:
            out.append(f)
    return out
