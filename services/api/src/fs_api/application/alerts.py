"""Alert queue use cases."""
from __future__ import annotations

from typing import Any

from ..api.context import Ctx
from ..errors import conflict, not_found
from .common import alert_out, flagged_lookup, masked_location, precision_for


async def _shape(ctx: Ctx, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flagged = await flagged_lookup(ctx)
    out = []
    for r in rows:
        vin = str(r["vin"]).strip()
        lat, lon = masked_location(r.get("lat"), r.get("lon"), precision_for(ctx, vin, flagged))
        out.append(alert_out(r, lat, lon))
    return out


async def list_alerts(ctx: Ctx, *, statuses: list[str] | None, severity: str | None, rule: str | None, vin: str | None,
                      since: str | None, cursor: str | None, limit: int | None) -> dict[str, Any]:
    n = ctx.limit(limit)
    after = ctx.cursor_in("alerts", cursor)
    async with ctx.tx() as repos:
        rows = await repos.alerts.list_page(statuses=statuses, severity=severity, rule=rule, vin=vin, since=since,
                                            after=after, limit=n + 1)
    nxt = None
    if len(rows) > n:
        rows = rows[:n]
        nxt = ctx.cursor_out("alerts", [rows[-1]["opened_at"].isoformat(), str(rows[-1]["id"])])
    ctx.note_resources("alert")
    return {"items": await _shape(ctx, rows), "next_cursor": nxt}


async def get_alert(ctx: Ctx, alert_id: str) -> dict[str, Any]:
    async with ctx.tx() as repos:
        row = await repos.alerts.get(alert_id)
    if row is None:
        raise not_found("alert")
    return (await _shape(ctx, [row]))[0]


async def transition(ctx: Ctx, alert_id: str, target: str) -> dict[str, Any]:
    """ack: open -> ack; resolve: open|ack -> resolved. Repeating a transition is a no-op (idempotent); moving backwards is 409."""
    async with ctx.tx() as repos:
        row = await repos.alerts.get(alert_id)
        if row is None:
            raise not_found("alert")
        cur = row["status"]
        if cur == target:
            return (await _shape(ctx, [row]))[0]
        if target == "ack" and cur == "resolved":
            raise conflict("alert is already resolved")
        updated = await repos.alerts.set_status(alert_id, target, ctx.principal.sub)
        if updated is None:
            raise conflict("alert changed concurrently")
    return (await _shape(ctx, [updated]))[0]
