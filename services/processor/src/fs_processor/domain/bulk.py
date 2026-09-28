"""Bulk-lane pure helpers: harsh rates, depot clustering, vehicle status. No I/O."""
from __future__ import annotations

from collections.abc import Sequence

from fleetsentinel.v1 import state_pb2, telemetry_pb2
from fscore.algorithms import UnionFind, encode, neighbours

from .geofence import Geofence, inside

HARSH_EVENTS = frozenset({telemetry_pb2.HARSH_BRAKE, telemetry_pb2.HARSH_ACCEL, telemetry_pb2.OVERSPEED})


def is_harsh(evt: int) -> bool:
    return evt in HARSH_EVENTS


def derive_status(ev: telemetry_pb2.TelemetryEvent) -> int:
    if ev.evt == telemetry_pb2.TOW:
        return state_pb2.TOWED
    if ev.evt in (telemetry_pb2.CHARGING_START, telemetry_pb2.CHARGING_END):
        return state_pb2.CHARGING
    if not ev.ignition:
        return state_pb2.PARKED
    if ev.speed_kmh >= 8.0:
        return state_pb2.DRIVING
    return state_pb2.IDLING


def build_vehicle_state(
    ev: telemetry_pb2.TelemetryEvent,
    tenant_id: str,
    risk_score: float,
    open_alerts: int,
) -> state_pb2.VehicleState:
    st = state_pb2.VehicleState(
        vin=ev.vin, tenant_id=tenant_id, ts_ms=ev.ts_ms, lat=ev.lat, lon=ev.lon,
        speed_kmh=ev.speed_kmh, status=derive_status(ev), ignition=bool(ev.ignition),
        risk_score=risk_score, open_alerts=open_alerts, ingest_ts_ms=ev.ingest_ts_ms, oem=ev.oem,
    )
    if ev.HasField("soc_pct"):
        st.soc_pct = ev.soc_pct
    if ev.HasField("fuel_pct"):
        st.fuel_pct = ev.fuel_pct
    return st


def cluster_depots(
    points: Sequence[tuple[str, float, float]],
    approved: Sequence[Geofence],
    min_size: int = 3,
) -> list[list[str]]:
    """Union-Find over geohash-7 cells (+ neighbours): groups of idle vehicles parked together.
    Returns sorted VIN lists for clusters NOT inside an approved fence. Deterministic order."""
    if len(points) < min_size:
        return []
    cells = {vin: encode(lat, lon, 7) for vin, lat, lon in points}
    cell_members: dict[str, list[str]] = {}
    for vin, cell in cells.items():
        cell_members.setdefault(cell, []).append(vin)
    uf = UnionFind(vin for vin, _la, _lo in points)
    for vin, cell in cells.items():
        for other_cell in [cell, *neighbours(cell)]:
            for other in cell_members.get(other_cell, ()):
                uf.union(vin, other)
    out: list[list[str]] = []
    for comp in uf.components():
        if len(comp) < min_size:
            continue
        vins = sorted(comp)
        lat = sum(p[1] for p in points if p[0] in set(vins)) / len(vins)
        lon = sum(p[2] for p in points if p[0] in set(vins)) / len(vins)
        if inside(lat, lon, approved) is None:
            out.append(vins)
    out.sort()
    return out


def centroid(points: Sequence[tuple[str, float, float]], vins: Sequence[str]) -> tuple[float, float]:
    want = set(vins)
    sel = [(la, lo) for v, la, lo in points if v in want]
    return sum(la for la, _ in sel) / len(sel), sum(lo for _, lo in sel) / len(sel)
