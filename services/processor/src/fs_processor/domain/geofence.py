"""Watchlist geofence checks: circular approved areas + after-hours gate. Pure."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from fscore.algorithms.geo import haversine_km

NIGHT_START_H = 22
NIGHT_END_H = 6


@dataclass(frozen=True, slots=True)
class Geofence:
    name: str
    lat: float
    lon: float
    radius_km: float


def inside(ev_lat: float, ev_lon: float, fences: Sequence[Geofence]) -> Geofence | None:
    """First approved fence containing the point, else None. O(fences)."""
    for f in fences:
        if haversine_km(ev_lat, ev_lon, f.lat, f.lon) <= f.radius_km:
            return f
    return None


def is_after_hours(ts_ms: int, start_h: int = NIGHT_START_H, end_h: int = NIGHT_END_H) -> bool:
    """UTC hour-of-day gate. O(1)."""
    hour = (ts_ms // 3_600_000) % 24
    if start_h <= end_h:
        return start_h <= hour < end_h
    return hour >= start_h or hour < end_h


def watchlist_breach(
    vin: str,
    ev_lat: float,
    ev_lon: float,
    ts_ms: int,
    watchlist: frozenset[str],
    approved: Sequence[Geofence],
) -> bool:
    """True when a watchlisted vehicle is outside every approved fence after hours."""
    if vin not in watchlist:
        return False
    if not is_after_hours(ts_ms):
        return False
    return inside(ev_lat, ev_lon, approved) is None
