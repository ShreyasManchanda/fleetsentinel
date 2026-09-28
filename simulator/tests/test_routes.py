from __future__ import annotations

import numpy as np
from fs_simulator.constants import CENTER_LAT, CENTER_LON
from hypothesis import given, settings
from hypothesis import strategies as st


def test_library_size_and_geometry(routes):
    assert routes.n_routes == 60 * 84 == 5040
    assert routes.pts.shape[1] == 64 and (routes.length_km > 0).all()
    assert abs(routes.pts[..., 0].mean() - CENTER_LAT) < 0.1 and abs(routes.pts[..., 1].mean() - CENTER_LON) < 0.1
    # out-and-back: every route starts and ends at its depot
    first = routes.site_routes[:, 0]
    assert np.allclose(routes.pts[first, 0], routes.site_pos) and np.allclose(routes.pts[first, -1], routes.site_pos)


def test_position_endpoints_and_clamp(routes):
    r = np.array([0, 5, 100])
    lat0, lon0 = routes.position(r, np.zeros(3))
    latL, lonL = routes.position(r, routes.length_km[r] * 5)        # far past the end -> clamped to the end
    assert np.allclose(lat0, routes.pts[r, 0, 0]) and np.allclose(latL, routes.pts[r, -1, 0])
    assert np.allclose(lon0, routes.pts[r, 0, 1]) and np.allclose(lonL, routes.pts[r, -1, 1])


@settings(max_examples=50, deadline=None)
@given(frac=st.floats(0, 1), idx=st.integers(0, 5039))
def test_position_stays_inside_route_bbox(routes, frac, idx):
    lat, lon = routes.position(np.array([idx]), np.array([frac * routes.length_km[idx]]))
    p = routes.pts[idx]
    assert p[:, 0].min() - 1e-9 <= lat[0] <= p[:, 0].max() + 1e-9
    assert p[:, 1].min() - 1e-9 <= lon[0] <= p[:, 1].max() + 1e-9


def test_random_nodes(routes):
    lat, lon = routes.random_node_latlon(np.random.default_rng(1), 10)
    assert len(lat) == 10 and set(lat.tolist()) <= set(routes.grid_lat.tolist())
