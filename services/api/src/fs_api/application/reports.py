"""Cost report (continuous aggregates) and predefined DuckDB batch reports (never free-form SQL)."""
from __future__ import annotations

from datetime import date
from typing import Any

from ..api.context import Ctx
from ..errors import unprocessable

REPORT_PARAMS: dict[str, dict[str, type]] = {
    "fuel_idle_by_depot": {"from": str, "to": str, "min_idle_min": int},
    "risk_by_sw_version": {"from": str, "to": str},
    "utilization_by_hour": {"from": str, "to": str},
    "carbon_by_fleet": {"from": str, "to": str},
}


def validate_report_params(report_id: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """Only whitelisted, correctly-typed params reach the query layer (they are bound, never interpolated)."""
    allowed = REPORT_PARAMS[report_id]
    out: dict[str, Any] = {}
    for k, v in (params or {}).items():
        if k not in allowed:
            raise unprocessable(f"unknown parameter '{k}' for report {report_id}", "invalid-report-params")
        typ = allowed[k]
        if typ is int and (isinstance(v, bool) or not isinstance(v, int)):
            raise unprocessable(f"parameter '{k}' must be an integer", "invalid-report-params")
        if typ is str:
            if not isinstance(v, str):
                raise unprocessable(f"parameter '{k}' must be a date string", "invalid-report-params")
            try:
                date.fromisoformat(v)
            except ValueError:
                raise unprocessable(f"parameter '{k}' must be YYYY-MM-DD", "invalid-report-params") from None
        out[k] = v
    return out


async def cost_report(ctx: Ctx, date_from: date, date_to: date, group_by: str) -> dict[str, Any]:
    if date_to < date_from:
        raise unprocessable("'to' must not be before 'from'", "invalid-range")
    async with ctx.tx() as repos:
        r = await repos.reports.cost_report(date_from.isoformat(), date_to.isoformat(), group_by)
    ctx.note_resources("report")
    return {"currency": "USD", **r}


async def batch_report(ctx: Ctx, report_id: str, params: dict[str, Any] | None) -> dict[str, Any]:
    clean = validate_report_params(report_id, params)
    async with ctx.tx() as repos:
        vins = await repos.reports.tenant_vins()  # tenant scope comes from RLS, not from the request
    ctx.note_resources("report", [report_id])
    return await ctx.deps.reports.run(report_id, clean, vins)
