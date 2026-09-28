from __future__ import annotations

import numpy as np
from fs_simulator.behaviour import FleetSim, SimConfig
from fs_simulator.groundtruth import GroundTruth

T0 = 1_790_000_000.0        # 2026-09-21 ~14:13 UTC
def make_sim(fleet, routes, seed=1, cfg=None, gt=None, t0=T0, subset=None):
    gt = GroundTruth(fleet.vin) if gt is None else gt
    cfg = cfg or SimConfig(emit_driving_s=60, emit_idle_s=60, emit_parked_s=60, emit_charging_s=60)
    g = np.arange(fleet.n) if subset is None else subset
    return FleetSim(fleet, routes, g, seed, t0, cfg, gt), gt


def run(sim, ticks, dt=60.0):
    return [sim.step(dt) for _ in range(ticks)]
