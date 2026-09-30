"""Explainable driver safety score (spec 10.2). Pure + tested.

The score is a weighted formula normalized per 100 km of exposure (long-haul
drivers are not penalized for driving more), adjusted for night share and road
class. Drivers below ``MIN_EXPOSURE_KM`` get a ``low`` confidence badge.
Coaching answers "what should change": the one or two behaviours contributing
most, expressed as peer-percentile deltas.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

MIN_EXPOSURE_KM = 300.0
NIGHT_START_H = 22
NIGHT_END_H = 6

# Penalty points per unit of exposure-normalised rate.
W_HARSH_BRAKE = 6.0
W_HARSH_ACCEL = 4.0
W_OVERSPEED_MIN = 0.8
W_IDLE_SHARE = 10.0
ROAD_CLASS_FACTOR = {"urban": 1.0, "mixed": 0.9, "highway": 0.8}

METRICS = ("harsh_brake", "harsh_accel", "overspeed_min", "idle_share")


@dataclass(slots=True)
class DriverExposure:
    harsh_brake_count: float = 0.0
    harsh_accel_count: float = 0.0
    overspeed_min: float = 0.0
    night_km: float = 0.0
    idle_hours_while_assigned: float = 0.0
    assigned_hours: float = 0.0
    km: float = 0.0
    road_class: str = "urban"


@dataclass(slots=True)
class SafetyScore:
    score: float  # 0..100
    confidence: str  # "ok" | "low"
    per_100km: dict[str, float] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)


def per_100km_rates(d: DriverExposure) -> dict[str, float]:
    km = max(d.km, 1e-9)
    idle_share = (d.idle_hours_while_assigned / d.assigned_hours) if d.assigned_hours > 0 else 0.0
    return {
        "harsh_brake": d.harsh_brake_count / km * 100.0,
        "harsh_accel": d.harsh_accel_count / km * 100.0,
        "overspeed_min": d.overspeed_min / km * 100.0,
        "night_share": d.night_km / km,
        "idle_share": idle_share,
    }


def compute_safety_score(d: DriverExposure) -> SafetyScore:
    """Weighted 0..100 score; night driving amplifies harsh-event penalties."""
    r = per_100km_rates(d)
    night_mult = 1.0 + 0.5 * min(max(r["night_share"], 0.0), 1.0)
    road = ROAD_CLASS_FACTOR.get(d.road_class, 1.0)
    penalties = {
        "harsh_brake": r["harsh_brake"] * W_HARSH_BRAKE * night_mult * road,
        "harsh_accel": r["harsh_accel"] * W_HARSH_ACCEL * night_mult * road,
        "overspeed_min": r["overspeed_min"] * W_OVERSPEED_MIN * night_mult,
        "idle_share": max(r["idle_share"], 0.0) * W_IDLE_SHARE,
    }
    score = float(np.clip(100.0 - sum(penalties.values()), 0.0, 100.0))
    confidence = "ok" if d.km >= MIN_EXPOSURE_KM else "low"
    return SafetyScore(score=score, confidence=confidence, per_100km=r, penalties=penalties)


def peer_p50(rates: dict[str, np.ndarray]) -> dict[str, float]:
    """Fleet p50 per metric (peer baseline for coaching deltas)."""
    return {m: float(np.percentile(np.asarray(v, dtype=float), 50)) if len(v) else 0.0 for m, v in rates.items()}


@dataclass(slots=True)
class CoachingLever:
    lever: str
    value: float
    fleet_p50: float
    message: str


_LEVER_WEIGHT = {"harsh_brake": W_HARSH_BRAKE, "harsh_accel": W_HARSH_ACCEL, "overspeed_min": W_OVERSPEED_MIN, "idle_share": W_IDLE_SHARE}
_LEVER_UNIT = {"harsh_brake": "/100 km", "harsh_accel": "/100 km", "overspeed_min": " min/100 km", "idle_share": " share"}


def coaching_levers(driver_rates: dict[str, float], fleet_med: dict[str, float], top_n: int = 2) -> list[CoachingLever]:
    """Top levers = largest (driver - p50) * weight gaps; empty when at/above peer median."""
    scored = []
    for m in METRICS:
        gap = driver_rates.get(m, 0.0) - fleet_med.get(m, 0.0)
        if gap > 0:
            scored.append((gap * _LEVER_WEIGHT[m], m, gap))
    scored.sort(reverse=True)
    out = []
    for _score, m, _gap in scored[:top_n]:
        v, p50 = driver_rates.get(m, 0.0), fleet_med.get(m, 0.0)
        out.append(CoachingLever(
            lever=m, value=round(v, 3), fleet_p50=round(p50, 3),
            message=f"{m.replace('_', ' ')} {v:.2f}{_LEVER_UNIT[m]} vs fleet p50 of {p50:.2f} — the biggest improvement lever",
        ))
    return out
