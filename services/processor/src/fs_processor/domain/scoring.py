"""ML scoring with automatic fallback to the rule baseline. Pure apart from model load.

Fallback rule (spec 8.6): any active DTC => high risk. A ``model_fallback_active`` gauge
is exposed via the caller-provided setter so the degraded state is visible.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

BASELINE_VERSION = "rule-baseline-v1"
BASELINE_HIGH = 0.9
BASELINE_LOW = 0.15
RISK_THRESHOLD = 0.7


def baseline_score(dtc_active: bool) -> float:
    return BASELINE_HIGH if dtc_active else BASELINE_LOW


@dataclass(slots=True)
class Features:
    dtc_count: int = 0
    harsh_count: int = 0
    idle_ratio: float = 0.0
    km_since_service: float = 0.0

    def vector(self) -> list[float]:
        return [float(self.dtc_count), float(self.harsh_count), self.idle_ratio, self.km_since_service]


class RiskScorer:
    """Tries a LightGBM booster; on missing artifact or any error uses the rule baseline."""

    def __init__(
        self,
        model_path: str | None = None,
        model_version: str = BASELINE_VERSION,
        on_fallback: Callable[[bool], None] | None = None,
    ) -> None:
        self.model_version = model_version
        self.fallback = True
        self._booster = None
        if model_path:
            self._try_load(model_path)
        if on_fallback is not None:
            on_fallback(self.fallback)

    def _try_load(self, path: str) -> None:
        try:
            import lightgbm as lgb

            self._booster = lgb.Booster(model_file=path)
            self.fallback = False
            self.model_version = path
        except Exception:
            self._booster = None
            self.fallback = True

    def score(self, features: Features, dtc_active: bool) -> tuple[float, str]:
        """Returns (risk 0..1, model_version). Never raises: model errors fall back."""
        if self._booster is not None:
            try:

                p = float(self._booster.predict([features.vector()])[0])
                return min(max(p, 0.0), 1.0), self.model_version
            except Exception:
                self._booster = None
                self.fallback = True
        return baseline_score(dtc_active), BASELINE_VERSION

    def is_high_risk(self, features: Features, dtc_active: bool) -> tuple[bool, float, str]:
        score, version = self.score(features, dtc_active)
        return score >= RISK_THRESHOLD, score, version
