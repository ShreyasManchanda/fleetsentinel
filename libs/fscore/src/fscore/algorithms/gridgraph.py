"""Synthetic city grid used as a stand-in road network when no OSM graph is available."""
from __future__ import annotations

import math
from collections.abc import Callable, Hashable, Iterable

from .geo import haversine_km
from .graph import RoadGraph


def grid_graph(
    lat0: float, lon0: float, lat1: float, lon1: float, step_km: float,
    cost_per_km: float = 0.18, detour: float = 1.25,
    rate_fn: Callable[[int, int, int, int], float] | None = None,
) -> tuple[RoadGraph, Callable[[float, float], Hashable]]:
    """8-connected grid over the bounding box. Returns (graph, snap); ``snap(lat, lon)`` is O(1) nearest node."""
    dlat = step_km / 111.195
    mid = math.radians((lat0 + lat1) / 2)
    dlon = step_km / (111.195 * max(math.cos(mid), 1e-6))
    nr = max(1, int((lat1 - lat0) / dlat)) + 1
    nc = max(1, int((lon1 - lon0) / dlon)) + 1
    g = RoadGraph()
    for r in range(nr):
        for c in range(nc):
            g.add_node((r, c), lat0 + r * dlat, lon0 + c * dlon)
    for r in range(nr):
        for c in range(nc):
            for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
                r2, c2 = r + dr, c + dc
                if 0 <= r2 < nr and 0 <= c2 < nc:
                    length = haversine_km(*g.pos[(r, c)], *g.pos[(r2, c2)]) * detour
                    mult = rate_fn(r, c, r2, c2) if rate_fn else 1.0
                    g.add_edge((r, c), (r2, c2), length, length * cost_per_km * mult)

    def snap(lat: float, lon: float) -> Hashable:
        r = min(max(round((lat - lat0) / dlat), 0), nr - 1)
        c = min(max(round((lon - lon0) / dlon), 0), nc - 1)
        return (r, c)

    return g, snap


def path_cost(g: RoadGraph, path: Iterable[Hashable]) -> float:
    """Sum of edge costs along a path (cheapest parallel edge). Test helper."""
    p = list(path)
    return sum(min(c for v, c in g.adj[a] if v == b) for a, b in zip(p, p[1:], strict=False))
