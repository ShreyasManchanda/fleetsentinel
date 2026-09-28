"""Vectorised per-vehicle behaviour engine: Markov states, shift schedules, route following, fuel/SoC, wear-driven DTCs.

One FleetSim advances a slice of the fleet (struct-of-arrays, no per-vehicle Python loops). Time is absolute epoch
seconds; step(dt) advances every vehicle by dt and returns an Emission (the records due this tick).
Faults/noise live in faults.py (mixin); the hidden wear state lives in wear.py.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .constants import (
    DTC_EV,
    DTC_ICE,
    DTC_INDEX,
    MODELS,
    PT_EV,
    PT_HYBRID,
    PT_ICE,
)
from .dynamics import DynamicsMixin
from .faults import FaultMixin
from .fleet import Fleet
from .groundtruth import GroundTruth
from .routes import RouteLibrary
from .wear import WearModel

MODEL_CAP = np.array([m[3] or 0.0 for m in MODELS])
MODEL_TANK = np.array([m[6] for m in MODELS])
MODEL_IDLE_L = np.array([m[4] or 0.0 for m in MODELS])
MODEL_IDLE_KW = np.array([m[5] or 0.0 for m in MODELS])
_ICE_POOL = np.array([DTC_INDEX[c] for c in DTC_ICE])
_EV_POOL = np.array([DTC_INDEX[c] for c in DTC_EV])
CRIT_ICE, CRIT_EV = DTC_INDEX["P0217"], DTC_INDEX["P0AA6"]


@dataclass
class SimConfig:
    emit_driving_s: float = 10.0
    emit_idle_s: float = 60.0
    emit_parked_s: float = 300.0
    emit_charging_s: float = 120.0
    burst_factor: float = 3.0
    burst_local_hour: float = 8.0
    burst_len_s: float = 1200.0
    faults: bool = True
    fault_scale: float = 1.0
    plan_days: float = 30.0
    gps_jump_p: float = 1e-4
    skew_share: float = 0.003
    dark_share_pending: float = 1.0


@dataclass
class Emission:
    """Records due this tick, struct-of-arrays. idx = local vehicle index within the FleetSim slice."""
    idx: np.ndarray
    gidx: np.ndarray
    ts_ms: np.ndarray
    seq: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    speed: np.ndarray
    soc: np.ndarray        # NaN where not applicable
    fuel: np.ndarray       # NaN where not applicable
    soh: np.ndarray        # NaN where not applicable
    odo: np.ndarray
    ign: np.ndarray
    evt: np.ndarray
    dtc: np.ndarray        # index into DTC_ALL or -1
    state: np.ndarray

    def __len__(self) -> int:
        return int(self.idx.shape[0])

    def select(self, mask: np.ndarray) -> Emission:
        return Emission(**{k: getattr(self, k)[mask] for k in self.__dataclass_fields__})


class FleetSim(DynamicsMixin, FaultMixin):
    def __init__(self, fleet: Fleet, routes: RouteLibrary, gidx: np.ndarray, seed: int, t0_s: float,
                 cfg: SimConfig | None = None, gt: GroundTruth | None = None, block_id: int = 0):
        self.fleet, self.routes, self.g = fleet, routes, np.asarray(gidx, dtype=np.int64)
        self.cfg = cfg or SimConfig()
        self.gt = gt
        self.block_id = block_id
        n = self.n = len(self.g)
        rng = self.rng = np.random.default_rng([seed, 3, block_id])
        self.t = float(t0_s)
        self.t0 = float(t0_s)
        pt = fleet.powertrain[self.g]
        mi = fleet.model_idx[self.g]
        self.is_ev, self.has_batt, self.has_fuel = pt == PT_EV, pt != PT_ICE, pt != PT_EV
        self.is_hybrid = pt == PT_HYBRID
        self.cap, self.tank = MODEL_CAP[mi], MODEL_TANK[mi]
        self.idle_l, self.idle_kw = MODEL_IDLE_L[mi], MODEL_IDLE_KW[mi]
        self.cons_l_km = np.where(self.is_hybrid, 0.6, 1.0) * self.tank / 600.0
        self.kwh_km = self.cap / 350.0
        self.home = fleet.home_site[self.g]
        self.aggr = fleet.driver_aggr[fleet.driver_idx[self.g]]
        night = rng.random(n) < 0.15
        self.shift_start = np.where(night, rng.normal(20.0, 1.5, n), np.clip(rng.normal(8.5, 1.2, n), 5.0, 12.0))
        self.shift_len = rng.uniform(8.0, 10.5, n)
        self.works = rng.random(n) < 0.85
        self.day = -1
        self.state = np.zeros(n, dtype=np.int8)
        self.mode = np.zeros(n, dtype=np.int8)          # 0 normal, 1 frozen, 2 moving override
        self.mode_until = np.zeros(n)
        self.frozen_kind = np.zeros(n, dtype=np.int8)
        self.lat = routes.site_pos[self.home, 0].copy()
        self.lon = routes.site_pos[self.home, 1].copy()
        self.route = routes.site_routes[self.home, 0].copy()
        self.s_km = np.zeros(n)
        self.speed = np.zeros(n)
        self.odo = rng.uniform(5_000, 250_000, n)
        self.odo_off = np.zeros(n)
        self.soc = np.where(self.has_batt, rng.uniform(50, 100, n), np.nan)
        self.fuel = np.where(self.has_fuel, rng.uniform(30, 100, n), np.nan)
        self.chg_target = rng.uniform(85, 100, n)
        self.ign = np.zeros(n, dtype=bool)
        self.seq = rng.integers(1_000, 100_000, n).astype(np.int64)
        self.rest_until = np.zeros(n)
        self.idle_until = np.zeros(n)
        self.trip_done = np.zeros(n, dtype=bool)
        self.trip_active = np.zeros(n, dtype=bool)
        self._trip_zero(np.ones(n, dtype=bool))
        self.trips: list[dict[str, np.ndarray]] = []
        self.wear = WearModel(rng, n, self.has_batt)
        self.dtc_code = np.full(n, -1, dtype=np.int16)
        self.dtc_until = np.zeros(n)
        self.day_km = np.zeros(n)
        self.next_emit = self.t + rng.uniform(0.0, cfg_period(self.cfg), n)
        self.ts_off = rng.uniform(0.0, 4.0, n)
        self._init_faults()

    def _trip_zero(self, m: np.ndarray) -> None:
        for name in ("trip_t0", "trip_dist", "trip_idle", "trip_energy", "trip_hb", "trip_ha", "trip_over", "trip_night"):
            if not hasattr(self, name):
                setattr(self, name, np.zeros(self.n))
            getattr(self, name)[m] = 0.0


def cfg_period(cfg: SimConfig) -> float:
    return max(cfg.emit_parked_s, cfg.emit_driving_s)
