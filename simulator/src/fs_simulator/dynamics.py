"""Markov state transitions, kinematics, fuel/SoC, trips and emission for FleetSim (mixin, fully vectorised)."""
from __future__ import annotations

import numpy as np

from .constants import CHARGING, DRIVING, DTC_ALL, EVT, IDLING, IST_OFFSET_S, PARKED
from .wear import dtc_rate_per_hour


def _p(dt: float, tau: float) -> float:
    return float(1.0 - np.exp(-dt / tau))


class DynamicsMixin:
    def _new_day(self, day: int) -> None:
        first = self.day < 0
        self.day = day
        n, rng = self.n, self.rng
        if not first:
            usage = self.day_km / max(float(self.day_km.mean()), 1.0)
            self.wear.advance_day(rng, usage)
        self.day_km[:] = 0.0
        weekday = (day + 3) % 7                       # 1970-01-01 was a Thursday; Monday=0
        self.works = rng.random(n) < (0.9 if weekday < 5 else 0.55)
        if self.cfg.faults:
            day_start = day * 86400.0 - IST_OFFSET_S
            mask, delay = self.wear.sample_breakdowns(rng, self.mode == 0)
            self.pend_break = np.where(mask, day_start + delay, np.inf)

    def _start_trips(self, m: np.ndarray, t: float, evt: np.ndarray) -> None:
        k = int(m.sum())
        if not k:
            return
        self.route[m] = self.routes.site_routes[self.home[m], self.rng.integers(0, self.routes.per_site, k)]
        self.s_km[m] = 0.0
        self.state[m] = DRIVING
        self.ign[m] = True
        self.trip_active[m] = True
        self.trip_done[m] = False
        self._trip_zero(m)
        self.trip_t0[m] = t
        evt[m] = EVT["IGNITION_ON"]

    def _end_trips(self, m: np.ndarray, t: float, evt: np.ndarray) -> None:
        k = int(m.sum())
        if not k:
            return
        rec = m & self.trip_active
        if rec.any():
            self.trips.append({
                "gidx": self.g[rec], "start_s": self.trip_t0[rec].copy(), "end_s": np.full(int(rec.sum()), t),
                "dist_km": self.trip_dist[rec].copy(), "idle_s": self.trip_idle[rec].copy(),
                "energy": self.trip_energy[rec].copy(), "hb": self.trip_hb[rec].copy(), "ha": self.trip_ha[rec].copy(),
                "over_s": self.trip_over[rec].copy(), "night_share": self.trip_night[rec] / np.maximum(t - self.trip_t0[rec], 1.0),
            })
        self.trip_active[m] = False
        self.state[m] = PARKED
        self.ign[m] = False
        self.speed[m] = 0.0
        self.lat[m] = self.routes.site_pos[self.home[m], 0]
        self.lon[m] = self.routes.site_pos[self.home[m], 1]
        self.rest_until[m] = t + self.rng.uniform(1800, 9000, k)
        evt[m] = EVT["IGNITION_OFF"]

    def _markov(self, t: float, dt: float, evt: np.ndarray) -> None:
        n, rng, st = self.n, self.rng, self.state
        lh = ((t + IST_OFFSET_S) % 86400.0) / 3600.0
        in_shift = (((lh - self.shift_start) % 24.0) < self.shift_len) & self.works
        normal = self.mode == 0
        want = normal & (st == PARKED) & in_shift & (t >= self.rest_until) & (rng.random(n) < _p(dt, 240.0))
        low = self.is_ev & (self.soc < 25.0)
        night_chg = normal & (st == PARKED) & ~in_shift & self.is_ev & (self.soc < 80.0) & (rng.random(n) < _p(dt, 1800.0))
        to_chg = (want & low) | night_chg
        self._start_trips(want & ~low, t, evt)
        st[to_chg] = CHARGING
        evt[to_chg] = EVT["CHARGING_START"]
        self.chg_target[to_chg] = rng.uniform(85, 100, int(to_chg.sum()))
        done_chg = normal & (st == CHARGING) & ~to_chg & ((self.soc >= self.chg_target) | (in_shift & (self.soc >= 60.0)))
        st[done_chg] = PARKED
        evt[done_chg] = EVT["CHARGING_END"]
        drv = normal & (st == DRIVING)
        arrive = drv & (self.s_km >= self.routes.length_km[self.route])
        idle_after = arrive & (rng.random(n) < 0.25)
        st[idle_after] = IDLING
        self.idle_until[idle_after] = t + rng.uniform(120, 900, int(idle_after.sum()))
        self.trip_done[idle_after] = True
        self._end_trips(arrive & ~idle_after, t, evt)
        mid = drv & ~arrive & (rng.random(n) < _p(dt, 1500.0))
        st[mid] = IDLING
        self.idle_until[mid] = t + rng.exponential(240.0, int(mid.sum()))
        idl = normal & (st == IDLING) & ~idle_after & ~mid & (t >= self.idle_until)
        self._end_trips(idl & self.trip_done, t, evt)
        st[idl & ~self.trip_done] = DRIVING

    def _kinematics(self, t: float, dt: float, evt: np.ndarray) -> None:
        n, rng, st = self.n, self.rng, self.state
        normal = self.mode == 0
        drv = normal & (st == DRIVING)
        idx = np.flatnonzero(drv)
        prev_all = self.speed.copy()
        self.speed[:] = 0.0
        if idx.size:
            target = self.routes.speed_kmh[self.route[idx]] * np.clip(rng.normal(1.0, 0.22, idx.size), 0.15, 1.5)
            prev = prev_all[idx]
            self.speed[idx] = 0.4 * target + 0.6 * np.maximum(prev, 0.5 * target)
            remaining = np.maximum(self.routes.length_km[self.route[idx]] - self.s_km[idx], 0.0)
            dist = np.minimum(self.speed[idx] * dt / 3600.0, remaining)
            self.s_km[idx] += dist
            self.odo[idx] += dist
            self.day_km[idx] += dist
            self.trip_dist[idx] += dist
            hrate = (0.15 + 4.0 * self.aggr[idx]) * dt / 3600.0
            u = rng.random(idx.size)
            hb = u < hrate * 0.55
            ha = (u >= hrate * 0.55) & (u < hrate)
            evt[idx[hb]] = EVT["HARSH_BRAKE"]
            evt[idx[ha]] = EVT["HARSH_ACCEL"]
            self.trip_hb[idx[hb]] += 1
            self.trip_ha[idx[ha]] += 1
            over = self.speed[idx] > 75.0
            evt[idx[over & (evt[idx] == 0)]] = EVT["OVERSPEED"]
            self.trip_over[idx[over]] += dt
        mv = np.flatnonzero(normal & ((st == DRIVING) | (st == IDLING)))
        if mv.size:
            self.lat[mv], self.lon[mv] = self.routes.position(self.route[mv], self.s_km[mv])
        act = self.trip_active
        if act.any():
            self.trip_idle[act & (st == IDLING)] += dt
            lh = ((t + IST_OFFSET_S) % 86400.0) / 3600.0
            if lh >= 22.0 or lh < 5.0:
                self.trip_night[act] += dt

    def _resources(self, t: float, dt: float) -> None:
        n, rng, st = self.n, self.rng, self.state
        moving = st == DRIVING
        dist = np.where(moving, self.speed * dt / 3600.0, 0.0)
        idle_h = np.where(st == IDLING, dt / 3600.0, 0.0)
        # fuel (ICE + hybrid)
        burn_l = dist * self.cons_l_km + idle_h * self.idle_l
        fm = self.has_fuel
        self.fuel[fm] = self.fuel[fm] - burn_l[fm] / np.maximum(self.tank[fm], 1.0) * 100.0
        refuel = fm & (st == PARKED) & (self.fuel < 15.0)
        self.fuel[refuel] = rng.uniform(85, 100, int(refuel.sum()))
        self.fuel[fm] = np.clip(self.fuel[fm], 0.0, 100.0)
        # EV battery
        used_kwh = dist * self.kwh_km + np.where((st == IDLING) | moving, self.idle_kw * dt / 3600.0, 0.0)
        em = self.is_ev
        self.soc[em] -= used_kwh[em] / np.maximum(self.cap[em], 1.0) * 100.0
        chg = em & (st == CHARGING)
        self.soc[chg] += 22.0 * dt / 3600.0 / np.maximum(self.cap[chg], 1.0) * 100.0
        self.soc[em] = np.clip(self.soc[em], 0.0, 100.0)
        hm = self.is_hybrid
        if hm.any():
            self.soc[hm] = np.clip(self.soc[hm] + 0.3 * (60.0 - self.soc[hm]) * min(dt / 600.0, 1.0)
                                   + rng.normal(0.0, 1.5, int(hm.sum())) * np.sqrt(dt / 600.0), 25.0, 90.0)
        energy = np.where(self.is_ev, used_kwh, burn_l)
        self.trip_energy[self.trip_active] += energy[self.trip_active]

    def _dtc(self, t: float, dt: float, evt: np.ndarray) -> None:
        rng, n = self.rng, self.n
        p = 1.0 - np.exp(-dtc_rate_per_hour(self.wear.w) * dt / 3600.0)
        new = (t >= self.dtc_until) & (rng.random(n) < p) & (self.mode == 0)
        k = int(new.sum())
        if k:
            use_ev = self.is_ev[new] | (self.is_hybrid[new] & (rng.random(k) < 0.5))
            from .behaviour import _EV_POOL, _ICE_POOL
            code = np.where(use_ev, _EV_POOL[rng.integers(0, len(_EV_POOL), k)], _ICE_POOL[rng.integers(0, len(_ICE_POOL), k)])
            self.dtc_code[new] = code
            self.dtc_until[new] = t + rng.uniform(2 * 3600.0, 48 * 3600.0, k)
            evt[new & (evt == 0)] = EVT["DTC_SET"]

    def step(self, dt: float, collect: bool = True):
        from .behaviour import Emission
        self.t += dt
        t = self.t
        day = int((t + IST_OFFSET_S) // 86400)
        if day != self.day:
            self._new_day(day)
        evt = np.zeros(self.n, dtype=np.int8)
        if self.cfg.faults:
            self._triggers(t, evt)
        self._markov(t, dt, evt)
        self._kinematics(t, dt, evt)
        self._resources(t, dt)
        self._dtc(t, dt, evt)
        if self.cfg.faults:
            self._override_motion(t, evt)
        if not collect:
            return None
        cfg, st = self.cfg, self.state
        per = np.array([cfg.emit_parked_s, cfg.emit_driving_s, cfg.emit_idle_s, cfg.emit_charging_s])[st] / self._rate_mult(t)
        emit = ((t >= self.next_emit) | (evt != 0)) & (t >= self.dark_until if cfg.faults else True)
        idx = np.flatnonzero(emit)
        self.next_emit[idx] = t + per[idx] * self.rng.uniform(0.7, 1.0, idx.size)
        self.seq[idx] += 1
        ts_ms = ((t - np.minimum(self.ts_off[idx], dt)) * 1000.0).astype(np.int64)
        lat, lon, speed = self.lat[idx].copy(), self.lon[idx].copy(), self.speed[idx].astype(np.float32)
        odo = np.maximum(self.odo[idx] + self.odo_off[idx], 0.0)
        if cfg.faults:
            ts_ms = ts_ms + self.skew_ms[idx]
            self._emission_faults(idx, t, ts_ms, lat, lon, speed)
        code = np.where(t < self.dtc_until[idx], self.dtc_code[idx], -1).astype(np.int16)
        return Emission(idx, self.g[idx], ts_ms, self.seq[idx], lat, lon, speed, self.soc[idx].astype(np.float32),
                        self.fuel[idx].astype(np.float32), self.wear.soh[idx].astype(np.float32), odo, self.ign[idx],
                        evt[idx], code, st[idx].copy())

    def _rate_mult(self, t: float) -> float:
        lh = ((t + IST_OFFSET_S) % 86400.0) / 3600.0
        b = self.cfg.burst_local_hour
        return self.cfg.burst_factor if (self.cfg.faults and b <= lh < b + self.cfg.burst_len_s / 3600.0) else 1.0
