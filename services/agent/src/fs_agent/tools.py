"""Allowlisted copilot tools (spec 11.2). Server-side, tenant-scoped, read-only —
except propose_action, which creates a *pending* proposal for human approval.

Backends are injected callables so unit tests use fakes and the service wires the API.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    required_args: tuple[str, ...] = ()
    handler: Callable[..., dict] | None = field(default=None, compare=False)


READ_ONLY = frozenset({
    "get_fleet_summary", "get_vehicle", "list_alerts", "explain_risk",
    "find_lookalike_vehicles", "estimate_impact", "search_knowledge",
    "run_report", "draft_mapping",
})
WRITE_TOOLS = frozenset({"propose_action"})
ALLOWLIST = READ_ONLY | WRITE_TOOLS


class UnknownTool(Exception):
    pass


class BadToolArgs(Exception):
    pass


class ToolRegistry:
    def __init__(self, backends: dict[str, Callable[..., dict]] | None = None) -> None:
        self._backends = dict(backends or {})

    def call(self, tenant_id: str, name: str, args: dict) -> dict:
        """Tenant scope is injected here, never taken from LLM output."""
        if name not in ALLOWLIST:
            raise UnknownTool(name)
        try:
            backend = self._backends[name]
        except KeyError:
            raise UnknownTool(f"no backend wired for {name}") from None
        args = dict(args or {})
        args.pop("tenant_id", None)  # an LLM-supplied tenant must never override or crash the call
        missing = [a for a in _REQUIRED.get(name, ()) if a not in args]
        if missing:
            raise BadToolArgs(f"{name} missing args: {missing}")
        out = backend(tenant_id=tenant_id, **args)
        if not isinstance(out, dict):
            raise BadToolArgs(f"{name} backend must return a dict")
        return out

    def describe(self) -> list[dict]:
        return [{"name": n, "description": _DESCRIPTIONS[n], "write": n in WRITE_TOOLS}
                for n in sorted(ALLOWLIST)]


_REQUIRED = {
    "get_vehicle": ("vin",),
    "explain_risk": ("vin",),
    "find_lookalike_vehicles": ("vin",),
    "estimate_impact": ("alert_id",),
    "search_knowledge": ("query",),
    "run_report": ("report_id",),
    "propose_action": ("proposal",),
    "draft_mapping": ("oem", "samples"),
}

_DESCRIPTIONS = {
    "get_fleet_summary": "Fleet KPIs: vehicles, open alerts, leakage today.",
    "get_vehicle": "One vehicle: twin, state, open alerts, risk + reason codes.",
    "list_alerts": "Alert list with filters (severity, status, rule).",
    "explain_risk": "Why a vehicle is flagged: reason codes + contributions.",
    "find_lookalike_vehicles": "kNN over pre-failure signatures (pgvector).",
    "estimate_impact": "Dollar impact of an alert at creation tariffs.",
    "search_knowledge": "DTC knowledge base + resolved incidents.",
    "run_report": "Predefined DuckDB reports only (never free-form SQL).",
    "propose_action": "Create a PENDING proposal (human approval required).",
    "draft_mapping": "Draft an OEM mapping YAML from sample payloads (never auto-activated).",
}
