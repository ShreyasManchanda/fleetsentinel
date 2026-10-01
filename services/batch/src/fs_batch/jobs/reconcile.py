"""Reconciliation: simulator ground-truth incidents vs emitted alerts.

Scores the whole alert pipeline (precision/recall/latency), not just the ML model.
Ground truth rows: (kind, vin, start_ts_ms). Alerts: (vin, rule, event_ts_ms, detected_ts_ms).
Kind -> rule mapping covers the injected incident kinds.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Incident:
    kind: str
    vin: str
    start_ts_ms: int


@dataclass(frozen=True, slots=True)
class FiredAlert:
    vin: str
    rule: str
    event_ts_ms: int
    detected_ts_ms: int


@dataclass(frozen=True, slots=True)
class Reconciliation:
    precision: float
    recall: float
    matched: int
    missed: int
    false_positives: int
    p50_latency_ms: float | None
    p95_latency_ms: float | None


KIND_TO_RULES = {
    "TOW": ("TOW",),
    "CRASH": ("CRASH",),
    "GOING_DARK": ("GOING_DARK",),
    "DEPOT_CLUSTER": ("UNAPPROVED_DEPOT_CLUSTER",),
    "BREAKDOWN": ("MAINTENANCE_RISK", "CRITICAL_DTC"),
}

MATCH_WINDOW_MS = 600_000  # incident matches an alert on the same VIN within ±10 min


def reconcile(incidents: list[Incident], alerts: list[FiredAlert]) -> Reconciliation:
    by_vin: dict[str, list[FiredAlert]] = {}
    for a in alerts:
        by_vin.setdefault(a.vin, []).append(a)
    matched = missed = 0
    latencies: list[float] = []
    used: set[tuple[str, int]] = set()  # (vin, per-vin index): global indices collide across VINs
    for inc in incidents:
        rules = KIND_TO_RULES.get(inc.kind, ())
        hit = None
        for i, a in enumerate(by_vin.get(inc.vin, [])):
            if (inc.vin, i) in used or a.rule not in rules:
                continue
            if abs(a.event_ts_ms - inc.start_ts_ms) <= MATCH_WINDOW_MS:
                hit = (i, a)
                break
        if hit is None:
            missed += 1
        else:
            i, a = hit
            used.add((inc.vin, i))
            matched += 1
            latencies.append(max(a.detected_ts_ms - inc.start_ts_ms, 0))
    fps = len(alerts) - len(used)
    precision = matched / max(matched + fps, 1)
    recall = matched / max(matched + missed, 1)
    latencies.sort()
    p50 = latencies[len(latencies) // 2] if latencies else None
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
    return Reconciliation(precision, recall, matched, missed, fps, p50, p95)
