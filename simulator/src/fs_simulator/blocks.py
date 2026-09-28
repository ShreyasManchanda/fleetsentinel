"""Block simulation for the seed: one FleetSim per vehicle block, warm-up at a coarse step, lake rows in the lake window."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from .behaviour import Emission, FleetSim, SimConfig
from .fleet import Fleet
from .groundtruth import GroundTruth
from .lake import LakeWriter, vehicle_shard_of
from .noise import concat_emissions
from .routes import RouteLibrary


@dataclass
class Timeline:
    start_s: float          # first simulated instant (UTC midnight)
    lake_start_s: float
    lake_end_s: float       # the anchor: trips and lake rows stop here
    end_s: float            # anchor + label horizon: breakdowns after the anchor are simulated for labels only
    dt_warm: float = 900.0
    dt_lake: float = 600.0

    @classmethod
    def build(cls, anchor: str = "2026-09-28", trip_days: int = 30, lake_days: int = 7, horizon_days: int = 7,
              dt_warm: float = 900.0, dt_lake: float = 600.0) -> Timeline:
        end = datetime.fromisoformat(anchor).replace(tzinfo=UTC).timestamp()
        return cls(end - trip_days * 86400.0, end - lake_days * 86400.0, end, end + horizon_days * 86400.0, dt_warm, dt_lake)

    @property
    def horizon_days_f(self) -> float:
        return (self.end_s - self.lake_end_s) / 86400.0

    @property
    def total_days(self) -> float:
        return (self.end_s - self.start_s) / 86400.0

    def dt_at(self, t: float) -> float:
        return self.dt_warm if t + self.dt_warm <= self.lake_start_s else self.dt_lake


@dataclass
class BlockResult:
    block_id: int
    n_vehicles: int
    rows: int = 0
    ticks: int = 0
    seconds: float = 0.0
    gt: GroundTruth | None = None
    trips: list = field(default_factory=list)


def lake_config(tl: Timeline, faults: bool = True) -> SimConfig:
    d = tl.dt_lake
    return SimConfig(emit_driving_s=d, emit_idle_s=d, emit_parked_s=d, emit_charging_s=d, faults=faults, plan_days=tl.total_days)


def block_indices(fleet: Fleet, n_blocks: int = 16) -> list[np.ndarray]:
    vs = vehicle_shard_of(fleet)
    return [np.flatnonzero(vs == b) for b in range(n_blocks)]


def simulate_block(fleet: Fleet, routes: RouteLibrary, gidx: np.ndarray, seed: int, block_id: int, tl: Timeline,
                   lake: LakeWriter | None, cfg: SimConfig | None = None, shard: int | None = None,
                   flush_rows: int = 1_500_000) -> BlockResult:
    gt = GroundTruth(fleet.vin)
    sim = FleetSim(fleet, routes, gidx, seed, tl.start_s, cfg or lake_config(tl), gt, block_id)
    res = BlockResult(block_id, len(gidx), gt=gt)
    block_vins, oem_local = fleet.vin[gidx], fleet.oem_idx[gidx]
    pending: list[Emission] = []
    pending_rows = 0
    shard = block_id if shard is None else shard

    def flush() -> None:
        nonlocal pending, pending_rows
        if pending and lake is not None:
            em = concat_emissions(pending)
            lake.write(em, block_vins, oem_local[em.idx], shard)
        pending, pending_rows = [], 0

    t0 = time.perf_counter()
    while sim.t < tl.end_s - 1e-6:
        dt = tl.dt_at(sim.t)
        nxt = sim.t + dt
        em = sim.step(dt, tl.lake_start_s < nxt <= tl.lake_end_s + 1e-6)
        res.ticks += 1
        if em is not None and len(em):
            res.rows += len(em)
            if lake is not None:
                pending.append(em)
                pending_rows += len(em)
        if pending_rows >= flush_rows or (pending and nxt % 86400.0 < dt):
            flush()
    flush()
    res.seconds = time.perf_counter() - t0
    for tr in sim.trips:
        m = tr["end_s"] <= tl.lake_end_s
        if m.any():
            res.trips.append({k: v[m] for k, v in tr.items()})
    return res
