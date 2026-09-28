"""Alerts."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from ...application import alerts as svc
from ...domain.roles import Perm
from ...errors import unprocessable
from ..context import Ctx, require
from ..idempotency import run_idempotent
from ..models import Alert, AlertPage, RuleCode, Severity
from ._common import PROBLEM_RESPONSES, CursorQ, IdemH, LimitQ, dump

router = APIRouter(tags=["Alerts"], responses=PROBLEM_RESPONSES)
Read = Annotated[Ctx, Depends(require(Perm.FLEET_READ))]
Write = Annotated[Ctx, Depends(require(Perm.ALERT_WRITE))]
STATUSES = {"open", "ack", "resolved"}


@router.get("/v1/alerts", response_model=AlertPage, summary="Alert queue (keyset)")
async def list_alerts(ctx: Read, cursor: CursorQ = None, limit: LimitQ = None,
                      status: Annotated[str | None, Query(description="comma separated: open,ack,resolved")] = None,
                      severity: Severity | None = None, rule: RuleCode | None = None,
                      vin: Annotated[str | None, Query(pattern=r"^[A-HJ-NPR-Z0-9]{1,17}$")] = None,
                      since: datetime | None = None):
    statuses = None
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if not set(statuses) <= STATUSES:
            raise unprocessable("status must be a comma separated subset of open,ack,resolved", "invalid-filter")
    return AlertPage.model_validate(await svc.list_alerts(
        ctx, statuses=statuses, severity=severity, rule=rule, vin=vin, since=since.isoformat() if since else None,
        cursor=cursor, limit=limit))


@router.get("/v1/alerts/{id}", response_model=Alert)
async def get_alert(id: UUID, ctx: Read):
    ctx.note_resources("alert", [str(id)])
    return Alert.model_validate(await svc.get_alert(ctx, str(id)))


@router.post("/v1/alerts/{id}/ack", response_model=Alert, summary="Acknowledge (idempotent)")
async def ack(id: UUID, ctx: Write, idempotency_key: IdemH = None):
    async def run():
        return 200, dump(Alert, await svc.transition(ctx, str(id), "ack"))

    return await run_idempotent(ctx, None, run)


@router.post("/v1/alerts/{id}/resolve", response_model=Alert)
async def resolve(id: UUID, ctx: Write, idempotency_key: IdemH = None):
    async def run():
        return 200, dump(Alert, await svc.transition(ctx, str(id), "resolved"))

    return await run_idempotent(ctx, None, run)
