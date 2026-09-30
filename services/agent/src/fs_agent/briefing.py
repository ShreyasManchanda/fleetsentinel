"""Rules-generated daily briefing (works with NO LLM key — the default path).

Ranks opportunities across cost / maintenance / safety by expected dollar value and
returns a Briefing-shaped dict matching the API contract (source="rules").
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Opportunity:
    kind: str  # cost | maintenance | safety | ops
    title: str
    detail: str
    impact_usd: float


def rank_opportunities(opps: list[Opportunity], limit: int = 5) -> list[Opportunity]:
    return sorted(opps, key=lambda o: o.impact_usd, reverse=True)[:max(limit, 0)]


def build_briefing(opportunities: list[Opportunity], generated_at: str,
                   limit: int = 5) -> dict:
    top = rank_opportunities(opportunities, limit)
    total = round(sum(o.impact_usd for o in top), 2)
    items = [{"rank": i + 1, "kind": o.kind, "title": o.title, "detail": o.detail,
              "impact_usd": round(o.impact_usd, 2)} for i, o in enumerate(top)]
    head = f"{len(top)} actions worth ${total:,.2f} this week" if top else "Nothing needs attention today"
    return {"generated_at": generated_at, "source": "rules", "headline": head,
            "total_impact_usd": total, "items": items}


def opportunities_from_inputs(*, idle_leakage_usd: float, idle_detail: str = "",
                              risks: list[dict] | None = None,
                              safety_flags: list[dict] | None = None) -> list[Opportunity]:
    """Assemble opportunities from pipeline outputs (alerts, risk list, safety scores)."""
    out: list[Opportunity] = []
    if idle_leakage_usd > 0:
        out.append(Opportunity("cost", "Cut idle-time fuel burn",
                               idle_detail or "Idle hours above the fleet p25 benchmark.",
                               idle_leakage_usd))
    for r in risks or []:
        ev = float(r.get("expected_value_usd", 0.0))
        if ev > 0:
            out.append(Opportunity("maintenance", f"Inspect {r.get('vin', '?')} before failure",
                                   f"p={r.get('p_failure_7d', 0):.2f}; {r.get('reasons', '')}", ev))
    for s in safety_flags or []:
        out.append(Opportunity("safety", f"Coach driver {s.get('driver', '?')}",
                               s.get("lever", ""), float(s.get("avoided_usd", 0.0))))
    return out
