"""Hidden maintenance wear state w(t) (spec 6.1, NON-LEAKY ground truth).

w is a per-vehicle continuous random walk with shocks. It is independent of every label: nothing here reads a label.
Observable signals are stochastic functions OF w:
  * DTC emission rate      = dtc_rate_per_hour(w)
  * SoH (battery) decay    = soh_rate_pct_per_day(w) + noise
  * breakdown hazard       = hazard_per_day(w), realised with a jittered delay
The label (breakdown within H days after a prediction time) is derived ONLY from the realised breakdown times.
Features exposed to ML must come from emitted telemetry strictly before the prediction time (see features.py).
"""
from __future__ import annotations

import numpy as np

HAZARD_BASE_PER_DAY = 0.004
HAZARD_BETA = 4.0
HAZARD_CAP = 0.5


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def dtc_rate_per_hour(w: np.ndarray) -> np.ndarray:
    return 5e-4 + 0.05 * _sigmoid((w - 1.1) / 0.12)


def soh_rate_pct_per_day(w: np.ndarray) -> np.ndarray:
    return 0.004 + 0.06 * _sigmoid((w - 0.9) / 0.2)


def hazard_per_day(w: np.ndarray) -> np.ndarray:
    return np.minimum(HAZARD_BASE_PER_DAY * np.exp(HAZARD_BETA * (w - 1.0)), HAZARD_CAP)


class WearModel:
    def __init__(self, rng: np.random.Generator, n: int, has_battery: np.ndarray):
        self.n = n
        self.w = rng.gamma(4.0, 0.15, n)
        self.soh = np.where(has_battery, 100.0 - rng.gamma(2.0, 2.0, n), np.nan)
        self.has_battery = has_battery

    def advance_day(self, rng: np.random.Generator, usage: np.ndarray | float = 1.0) -> None:
        """One day of random-walk wear. usage is a non-negative relative-usage multiplier (km / mean km)."""
        shock = (rng.random(self.n) < 0.004) * rng.exponential(0.25, self.n)
        self.w = np.maximum(self.w + 0.004 * (0.5 + np.asarray(usage)) + rng.normal(0.0, 0.012, self.n) + shock, 0.0)
        decay = soh_rate_pct_per_day(self.w) * rng.lognormal(0.0, 0.3, self.n)
        self.soh = np.where(self.has_battery, np.maximum(self.soh - decay, 40.0), np.nan)

    def sample_breakdowns(self, rng: np.random.Generator, eligible: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Hazard draw for the coming day. Returns (mask, delay_s): the breakdown happens delay_s after the day start
        (lognormal, median about 10 h, jittered); the delay is independent of w."""
        p = 1.0 - np.exp(-hazard_per_day(self.w))
        mask = (rng.random(self.n) < p) & eligible
        delay_s = rng.lognormal(np.log(10 * 3600.0), 0.8, self.n)
        return mask, delay_s

    def repair(self, mask: np.ndarray) -> None:
        self.w = np.where(mask, 0.25 * self.w, self.w)
