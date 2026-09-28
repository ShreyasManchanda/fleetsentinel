"""Agent action proposals (approval queue). The agent may only create PENDING proposals; a human approves."""
from __future__ import annotations

from typing import Any

from ..api.context import Ctx
from ..errors import conflict, not_found


def action_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "status": row["status"], "proposal": row["proposal"], "rationale": row.get("rationale"),
        "expected_impact_usd": float(row["expected_impact_usd"]) if row.get("expected_impact_usd") is not None else None,
        "created_at": row.get("created_at"), "decided_at": row.get("decided_at"), "work_order_id": row.get("work_order_id"),
    }


async def list_actions(ctx: Ctx, status: str | None, cursor: str | None, limit: int | None) -> dict[str, Any]:
    n = ctx.limit(limit)
    after = ctx.cursor_in("actions", cursor)
    async with ctx.tx() as repos:
        rows = await repos.actions.list_page(status=status, after=after, limit=n + 1)
    nxt = None
    if len(rows) > n:
        rows = rows[:n]
        nxt = ctx.cursor_out("actions", [rows[-1]["created_at"].isoformat(), str(rows[-1]["id"])])
    ctx.note_resources("agent_action")
    return {"items": [action_out(r) for r in rows], "next_cursor": nxt}


async def create_action(ctx: Ctx, *, proposal: dict[str, Any], rationale: str, expected_impact_usd: float | None,
                        alert_id: str | None, idempotency_key: str) -> dict[str, Any]:
    async with ctx.tx() as repos:
        row = await repos.actions.create(proposal=proposal, rationale=rationale, expected_impact_usd=expected_impact_usd or 0.0,
                                         alert_id=alert_id, idempotency_key=idempotency_key)
    ctx.note_resources("agent_action", [str(row["id"])])
    return action_out(row)


async def approve(ctx: Ctx, action_id: str) -> dict[str, Any]:
    """pending -> approved (creates the work order). Approving twice returns the same result; approving a rejected one is 409."""
    async with ctx.tx() as repos:
        cur = await repos.actions.get(action_id)
        if cur is None:
            raise not_found("action")
        if cur["status"] == "approved":
            return action_out(cur)
        if cur["status"] != "pending":
            raise conflict(f"action is {cur['status']}, only pending actions can be approved")
        row = await repos.actions.decide(action_id, "approved", ctx.principal.sub, None)
        if row is None:
            raise conflict("action changed concurrently")
    return action_out(row)


async def reject(ctx: Ctx, action_id: str, reason: str | None) -> dict[str, Any]:
    async with ctx.tx() as repos:
        cur = await repos.actions.get(action_id)
        if cur is None:
            raise not_found("action")
        if cur["status"] == "rejected":
            return action_out(cur)
        if cur["status"] != "pending":
            raise conflict(f"action is {cur['status']}, only pending actions can be rejected")
        row = await repos.actions.decide(action_id, "rejected", ctx.principal.sub, reason)
        if row is None:
            raise conflict("action changed concurrently")
    return action_out(row)
