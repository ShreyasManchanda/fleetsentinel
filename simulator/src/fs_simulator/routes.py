"""Synthetic city-grid route library around Bengaluru (route library v1).

Routes are out-and-back polylines that start and end at a depot site. Every route is resampled to K points uniformly in
arc length so position lookup for N vehicles is two gathers and a lerp (fully vectorised).
OSM extraction of real Bengaluru roads is a stretch goal and is NOT implemented.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .constants import CENTER_LAT, CENTER_LON

M_PER_DEG_LAT = 111_320.0


@dataclass(slots=True)
class RouteLibrary:
    pts: np.ndarray            # (R, K, 2) lat, lon
    length_km: np.ndarray      # (R,)
    speed_kmh: np.ndarray      # (R,) typical road-class speed
    site_routes: np.ndarray    # (S, P) route indices per depot site
    site_pos: np.ndarray       # (S, 2) depot lat/lon
    grid_lat: np.ndarray       # (G,) node latitudes
    grid_lon: np.ndarray       # (G,) node longitudes
    site_nodes: np.ndarray     # (S, 2) grid (i, j) of the sites

    @property
    def n_routes(self) -> int:
        return int(self.pts.shape[0])

    @property
    def n_sites(self) -> int:
        return int(self.site_routes.shape[0])

    @property
    def per_site(self) -> int:
        return int(self.site_routes.shape[1])

    def position(self, route: np.ndarray, s_km: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Lat/lon at distance s_km along each route (clamped to the route)."""
        k = self.pts.shape[1]
        frac = np.clip(s_km / np.maximum(self.length_km[route], 1e-6), 0.0, 1.0) * (k - 1)
        i0 = np.minimum(frac.astype(np.int64), k - 2)
        w = (frac - i0)[:, None]
        a = self.pts[route, i0]
        b = self.pts[route, i0 + 1]
        p = a + (b - a) * w
        return p[:, 0], p[:, 1]

    def random_node_latlon(self, rng: np.random.Generator, n: int) -> tuple[np.ndarray, np.ndarray]:
        g = len(self.grid_lat)
        return self.grid_lat[rng.integers(0, g, n)], self.grid_lon[rng.integers(0, g, n)]


def _manhattan_path(rng: np.random.Generator, src: tuple[int, int], dst: tuple[int, int]) -> list[tuple[int, int]]:
    """Waypoints from src to dst alternating vertical/horizontal runs (1-3 turns)."""
    pts = [src]
    ci, cj = src
    di, dj = dst[0] - ci, dst[1] - cj
    n_turns = int(rng.integers(1, 4))
    i_chunks = np.array_split(np.arange(abs(di)), n_turns)
    j_chunks = np.array_split(np.arange(abs(dj)), n_turns)
    for a, b in zip(i_chunks, j_chunks, strict=True):
        ci += int(np.sign(di)) * len(a)
        pts.append((ci, cj))
        cj += int(np.sign(dj)) * len(b)
        pts.append((ci, cj))
    return pts


def build_library(
    seed: int = 7, n_sites: int = 60, per_site: int = 84, grid: int = 48, spacing_m: float = 450.0, k: int = 64
) -> RouteLibrary:
    """Build n_sites*per_site (5,040 by default) routes on a grid x grid synthetic city."""
    rng = np.random.default_rng([seed, 0xC17])
    lat_step = spacing_m / M_PER_DEG_LAT
    coslat = math.cos(math.radians(CENTER_LAT))
    lon_step = spacing_m / (M_PER_DEG_LAT * coslat)
    grid_lat = CENTER_LAT + (np.arange(grid) - grid / 2) * lat_step
    grid_lon = CENTER_LON + (np.arange(grid) - grid / 2) * lon_step
    flat = rng.choice(grid * grid, size=n_sites, replace=False)
    site_nodes = np.stack([flat // grid, flat % grid], axis=1)
    n_routes = n_sites * per_site
    routes = np.zeros((n_routes, k, 2))
    length = np.zeros(n_routes)
    for s in range(n_sites):
        src = (int(site_nodes[s, 0]), int(site_nodes[s, 1]))
        for r in range(per_site):
            blocks = int(rng.integers(3, 26))
            while True:
                di = int(rng.integers(-blocks, blocks + 1))
                dj = int(rng.integers(-blocks, blocks + 1))
                dst = (min(max(src[0] + di, 0), grid - 1), min(max(src[1] + dj, 0), grid - 1))
                if dst != src:
                    break
            way = _manhattan_path(rng, src, dst)
            way = way + way[-2::-1]                       # out and back to the depot
            xy = np.array([[grid_lat[i], grid_lon[j]] for i, j in way])
            seg_m = np.hypot(np.diff(xy[:, 0]) * M_PER_DEG_LAT, np.diff(xy[:, 1]) * M_PER_DEG_LAT * coslat)
            cum = np.concatenate([[0.0], np.cumsum(seg_m)])
            total = max(float(cum[-1]), 1.0)
            u = np.linspace(0.0, total, k)
            idx = s * per_site + r
            routes[idx, :, 0] = np.interp(u, cum, xy[:, 0])
            routes[idx, :, 1] = np.interp(u, cum, xy[:, 1])
            length[idx] = total / 1000.0
    # road class by length: short = local, medium = arterial, long = ring road
    speed = np.where(length < 5, 28.0, np.where(length < 14, 42.0, 58.0))
    site_routes = np.arange(n_routes).reshape(n_sites, per_site)
    site_pos = np.stack([grid_lat[site_nodes[:, 0]], grid_lon[site_nodes[:, 1]]], axis=1)
    return RouteLibrary(routes, length, speed, site_routes, site_pos, grid_lat, grid_lon, site_nodes)
