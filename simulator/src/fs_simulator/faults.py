"""Fault and noise injection, all recorded in ground truth: tow, crash, going dark, depot clusters, stuck speed sensor,
odometer rollback, clock skew, GPS jumps, breakdowns (from the hidden wear hazard) and 3x shift-start bursts.
Transport-level noise (out-of-order, duplicates, batch retries) lives in noise.py."""
from __future__ import annotations

import numpy as np

from . import groundtruth as G
from .constants import DRIVING, EVT, PARKED

RATES_PER_VEHICLE_DAY = {"tow": 2e-4, "crash": 1e-4, "dark": 1.5e-3, "stuck": 1e-3, "roll": 2e-4}
CLUSTERS_PER_10K_DAY = 0.6
KIND_TOW, KIND_CLUSTER, KIND_CRASH, KIND_BREAKDOWN = 1, 2, 3, 4


class FaultMixin:
    def _init_faults(self) -> None:
        n, rng, cfg = self.n, self.rng, self.cfg
        self.dark_until = np.zeros(n)
        self.stuck_until = np.zeros(n)
        self.stuck_speed = np.zeros(n)
        self.skew_ms = np.zeros(n, dtype=np.int64)
        self.pend_break = np.full(n, np.inf)
        self.pend = {k: np.full(n, np.inf) for k in RATES_PER_VEHICLE_DAY}
        self.ov = {k: np.zeros(n) for k in ("lat0", "lon0", "lat1", "lon1", "t0", "t1", "speed", "after")}
        self.ov_kind = np.zeros(n, dtype=np.int8)
        self.clusters: list[dict] = []
        if not cfg.faults:
            return
        days, sc = cfg.plan_days, cfg.fault_scale
        for kind, rate in RATES_PER_VEHICLE_DAY.items():
            k = int(rng.poisson(rate * n * days * sc))
            v = rng.integers(0, n, k)
            np.minimum.at(self.pend[kind], v, self.t0 + rng.uniform(0, days * 86400.0, k))
        sk = rng.random(n) < cfg.skew_share
        ns = int(sk.sum())
        self.skew_ms[sk] = (rng.choice([-1, 1], ns) * rng.uniform(60, 600, ns) * 1000).astype(np.int64)
        if self.gt is not None and ns:
            self.gt.add(G.CLOCK_SKEW, self.g[sk], int(self.t0 * 1000), value=self.skew_ms[sk])
        n_cl = int(rng.poisson(CLUSTERS_PER_10K_DAY * days * sc * n / 10_000))
        for i in range(n_cl):
            site = int(rng.choice(self.home))
            members = np.flatnonzero(self.home == site)
            if len(members) < 4:
                continue
            m = rng.choice(members, size=min(int(rng.integers(8, 31)), len(members)), replace=False)
            self.clusters.append({
                "start": self.t0 + float(rng.uniform(0, days * 86400.0)), "members": m, "dwell": float(rng.uniform(45, 180) * 60.0),
                "lat": float(self.routes.site_pos[site, 0] + rng.uniform(-0.03, 0.03)),
                "lon": float(self.routes.site_pos[site, 1] + rng.uniform(-0.03, 0.03)), "id": f"cl-{self.block_id}-{i}",
            })
        self.clusters.sort(key=lambda c: c["start"])

    def _freeze(self, m: np.ndarray, until, kind: int) -> None:
        self.mode[m] = 1
        self.mode_until[m] = until
        self.frozen_kind[m] = kind
        self.state[m] = PARKED
        self.ign[m] = False
        self.speed[m] = 0.0
        self.trip_active[m] = False

    def _start_move(self, m: np.ndarray, t: float, lat1, lon1, speed: float, ign: bool, after, kind: int) -> None:
        o = self.ov
        o["lat0"][m], o["lon0"][m] = self.lat[m], self.lon[m]
        o["lat1"][m], o["lon1"][m] = lat1, lon1
        dist_km = np.hypot((lat1 - self.lat[m]) * 111.32, (lon1 - self.lon[m]) * 108.5)
        o["t0"][m] = t
        o["t1"][m] = t + np.maximum(dist_km / speed * 3600.0, 60.0)
        o["speed"][m] = speed
        o["after"][m] = after
        self.ov_kind[m] = kind
        self.mode[m] = 2
        self.state[m] = PARKED
        self.ign[m] = ign
        self.trip_active[m] = False

    def _triggers(self, t: float, evt: np.ndarray) -> None:
        from .behaviour import CRIT_EV, CRIT_ICE
        rng, gt, g = self.rng, self.gt, self.g
        ms = int(t * 1000)
        normal = self.mode == 0
        # breakdown: hazard on the hidden wear w; the jittered delay was drawn at day start
        m = (self.pend_break <= t) & normal
        k = int(m.sum())
        if k:
            until = t + rng.uniform(0.5, 3.0, k) * 86400.0
            self._freeze(m, until, KIND_BREAKDOWN)
            self.dtc_code[m] = np.where(self.is_ev[m], CRIT_EV, CRIT_ICE)
            self.dtc_until[m] = until
            evt[m] = EVT["DTC_SET"]
            self.pend_break[m] = np.inf
            if gt is not None:
                gt.add(G.BREAKDOWN, g[m], ms, end_ms=(until * 1000).astype(np.int64), lat=self.lat[m], lon=self.lon[m])
        m = (self.pend["crash"] <= t) & normal & (self.state == DRIVING)
        if m.any():
            self._freeze(m, t + rng.uniform(2, 6, int(m.sum())) * 3600.0, KIND_CRASH)
            evt[m] = EVT["CRASH"]
            self.pend["crash"][m] = np.inf
            if gt is not None:
                gt.add(G.CRASH, g[m], ms, lat=self.lat[m], lon=self.lon[m])
        m = (self.pend["tow"] <= t) & normal & (self.state == PARKED)
        k = int(m.sum())
        if k:
            ang, d = rng.uniform(0, 2 * np.pi, k), rng.uniform(3.0, 12.0, k)
            self._start_move(m, t, self.lat[m] + d * np.sin(ang) / 111.32, self.lon[m] + d * np.cos(ang) / 108.5,
                             45.0, False, np.inf, KIND_TOW)
            evt[m] = EVT["TOW"]
            self.pend["tow"][m] = np.inf
            if gt is not None:
                gt.add(G.TOW, g[m], ms, end_ms=(self.ov["t1"][m] * 1000).astype(np.int64), lat=self.lat[m], lon=self.lon[m])
        m = self.pend["dark"] <= t
        k = int(m.sum())
        if k:
            dur = rng.uniform(20 * 60.0, 3 * 3600.0, k)
            self.dark_until[m] = t + dur
            self.pend["dark"][m] = np.inf
            if gt is not None:
                gt.add(G.GOING_DARK, g[m], ms, end_ms=((t + dur) * 1000).astype(np.int64), lat=self.lat[m], lon=self.lon[m])
        m = (self.pend["stuck"] <= t) & (self.state == DRIVING) & normal
        k = int(m.sum())
        if k:
            dur = rng.uniform(10 * 60.0, 60 * 60.0, k)
            self.stuck_until[m] = t + dur
            self.stuck_speed[m] = np.maximum(self.speed[m], 20.0)
            self.pend["stuck"][m] = np.inf
            if gt is not None:
                gt.add(G.STUCK_SPEED, g[m], ms, end_ms=((t + dur) * 1000).astype(np.int64), value=self.stuck_speed[m])
        m = self.pend["roll"] <= t
        k = int(m.sum())
        if k:
            delta = np.minimum(self.odo[m] + self.odo_off[m] - 100.0, rng.uniform(1000.0, 8000.0, k))
            self.odo_off[m] -= delta
            self.pend["roll"][m] = np.inf
            if gt is not None:
                gt.add(G.ODOMETER_ROLLBACK, g[m], ms, value=delta)
        while self.clusters and self.clusters[0]["start"] <= t:
            self._trigger_cluster(self.clusters.pop(0), t)

    def _trigger_cluster(self, c: dict, t: float) -> None:
        mem = c["members"]
        ok = mem[self.mode[mem] == 0]
        if not len(ok):
            return
        m = np.zeros(self.n, dtype=bool)
        m[ok] = True
        jit = self.rng.normal(0.0, 0.0006, (len(ok), 2))
        self._start_move(m, t, c["lat"] + jit[:, 0], c["lon"] + jit[:, 1], 15.0, True, np.inf, KIND_CLUSTER)
        self.ov["after"][m] = self.ov["t1"][m] + c["dwell"]
        if self.gt is not None:
            self.gt.add(G.DEPOT_CLUSTER, self.g[ok], int(t * 1000), end_ms=(self.ov["after"][m] * 1000).astype(np.int64),
                        lat=c["lat"], lon=c["lon"], group_id=c["id"], value=float(len(ok)))

    def _override_motion(self, t: float, evt: np.ndarray) -> None:
        o = self.ov
        mv = self.mode == 2
        if mv.any():
            frac = np.clip((t - o["t0"][mv]) / np.maximum(o["t1"][mv] - o["t0"][mv], 1.0), 0.0, 1.0)
            self.lat[mv] = o["lat0"][mv] + (o["lat1"][mv] - o["lat0"][mv]) * frac
            self.lon[mv] = o["lon0"][mv] + (o["lon1"][mv] - o["lon0"][mv]) * frac
            self.speed[mv] = np.where(frac < 1.0, o["speed"][mv], 0.0)
            done = mv & (t >= o["t1"])
            if done.any():
                self._freeze(done, o["after"][done], KIND_CLUSTER)
        fr = (self.mode == 1) & (t >= self.mode_until)
        if fr.any():
            bd = fr & (self.frozen_kind == KIND_BREAKDOWN)
            if bd.any():
                self.wear.repair(bd)
                self.dtc_until[bd] = 0.0
            self.mode[fr] = 0
            self.rest_until[fr] = t + self.rng.uniform(1800, 9000, int(fr.sum()))
            back = fr & (self.frozen_kind != KIND_TOW)
            self.lat[back] = self.routes.site_pos[self.home[back], 0]
            self.lon[back] = self.routes.site_pos[self.home[back], 1]

    def _emission_faults(self, idx: np.ndarray, t: float, ts_ms: np.ndarray, lat: np.ndarray, lon: np.ndarray, speed: np.ndarray) -> None:
        rng = self.rng
        if not len(idx):
            return
        j = rng.random(len(idx)) < self.cfg.gps_jump_p
        k = int(j.sum())
        if k:
            dlat = rng.choice([-1, 1], k) * rng.uniform(0.05, 0.4, k)
            lat[j] = lat[j] + dlat
            if self.gt is not None:
                self.gt.add(G.GPS_JUMP, self.g[idx[j]], ts_ms[j], lat=lat[j], lon=lon[j], value=np.abs(dlat) * 111.32)
        s = t < self.stuck_until[idx]
        if s.any():
            speed[s] = self.stuck_speed[idx[s]]
