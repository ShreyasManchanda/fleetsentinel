"""Compliance: audit log + verify, DSR (crypto-shredding), privacy-safe export."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from ...application import compliance as svc
from ...domain.roles import Perm
from ..context import Ctx, require
from ..idempotency import run_idempotent
from ..models import AuditPage, AuditVerify, Dsr, DsrCreate, InsightExport
from ._common import PROBLEM_RESPONSES, CursorQ, IdemH, LimitQ, dump

router = APIRouter(tags=["Compliance"], responses=PROBLEM_RESPONSES)
Export = Annotated[Ctx, Depends(require(Perm.INSIGHT_EXPORT))]
AuditG = Annotated[Ctx, Depends(require(Perm.AUDIT_READ))]
DsrG = Annotated[Ctx, Depends(require(Perm.DSR))]


@router.get("/v1/insights/export", response_model=InsightExport, summary="Privacy-safe aggregate export (geohash-5 + k-anonymity k>=10)")
async def insights_export(ctx: Export, from_: Annotated[date, Query(alias="from")], to: date):
    return InsightExport.model_validate(await svc.insight_export(ctx, from_, to))


@router.get("/v1/audit", response_model=AuditPage, summary="Audit log (tenant-scoped, keyset)")
async def audit(ctx: AuditG, cursor: CursorQ = None, limit: LimitQ = None, actor_id: Annotated[str | None, Query(max_length=128)] = None,
                action: Annotated[str | None, Query(max_length=200)] = None, decision: Literal["ALLOW", "DENY"] | None = None):
    return AuditPage.model_validate(await svc.list_audit(ctx, actor_id=actor_id, action=action, decision=decision, cursor=cursor, limit=limit))


@router.get("/v1/audit/verify", response_model=AuditVerify, summary="Verify the hash chain (calls audit_verify)")
async def audit_verify(ctx: AuditG, from_seq: Annotated[int, Query(ge=0)] = 0, to_seq: int | None = None):
    return AuditVerify.model_validate(await svc.verify_audit(ctx, from_seq, to_seq))


@router.post("/v1/dsr", response_model=Dsr, status_code=202, summary="Data-subject request: ERASE (crypto-shredding) or ACCESS (export)")
async def create_dsr(body: DsrCreate, ctx: DsrG, idempotency_key: IdemH = None):
    async def run():
        return 202, dump(Dsr, await svc.create_dsr(ctx, str(body.driver_id), body.kind))

    return await run_idempotent(ctx, body.model_dump(mode="json"), run, redact=svc.redact_dsr)


@router.get("/v1/dsr/{id}", response_model=Dsr)
async def get_dsr(id: UUID, ctx: DsrG):
    return Dsr.model_validate(await svc.get_dsr(ctx, str(id)))
