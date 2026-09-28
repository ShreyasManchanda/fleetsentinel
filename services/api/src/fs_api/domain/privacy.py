"""Privacy-safe aggregate export: geohash-5 generalisation + k-anonymity suppression (spec 12.1)."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .geo import geohash_encode

K_DEFAULT = 10
EXPORT_PRECISION = 5


@dataclass(slots=True)
class _Cell:
    vehicles: set[int] = field(default_factory=set)
    idle_s: float = 0.0
    harsh: int = 0


def k_anonymize(rows: Iterable[tuple[int, float, float, float, int]], k: int = K_DEFAULT) -> dict:
    """rows = (vehicle_key, lat, lon, idle_s, harsh_events). The vehicle_key is an opaque in-memory integer used ONLY to
    count DISTINCT vehicles per cell; it (and any VIN or driver id) never appears in the output.

    A cell with fewer than k distinct vehicles is suppressed. Totals cover the PUBLISHED cells only, so subtracting the
    totals from a grand total cannot reveal what a suppressed cell contained (differencing attack).
    """
    cells: dict[str, _Cell] = {}
    for vkey, lat, lon, idle_s, harsh in rows:
        c = cells.setdefault(geohash_encode(lat, lon, EXPORT_PRECISION), _Cell())
        c.vehicles.add(vkey)
        c.idle_s += idle_s
        c.harsh += harsh
    published, suppressed = [], 0
    for gh in sorted(cells):
        c = cells[gh]
        if len(c.vehicles) < k:
            suppressed += 1
            continue
        published.append({
            "geohash": gh,
            "vehicles_approx": len(c.vehicles),
            "idle_hours": round(c.idle_s / 3600.0, 2),
            "harsh_events": c.harsh,
        })
    totals = {
        "cells": float(len(published)),
        "vehicles_approx": float(sum(p["vehicles_approx"] for p in published)),
        "idle_hours": round(sum(p["idle_hours"] for p in published), 2),
        "harsh_events": float(sum(p["harsh_events"] for p in published)),
    }
    return {
        "k_anonymity": k,
        "location_precision": "geohash5",
        "cells": published,
        "suppressed_cells": suppressed,
        "totals": totals,
    }
