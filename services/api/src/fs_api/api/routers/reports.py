"""Reports (cost from continuous aggregates; predefined DuckDB batch reports)."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from ...application import reports as svc
from ...domain.roles import Perm
from ..context import Ctx, require
from ..models import BatchReportRequest, BatchReportResult, CostReport
from ._common import PROBLEM_RESPONSES

router = APIRouter(tags=["Reports"], responses=PROBLEM_RESPONSES)
Guard = Annotated[Ctx, Depends(require(Perm.REPORT_READ))]


@router.get("/v1/reports/cost", response_model=CostReport, summary="Cost leakage and savings opportunity")
async def cost(ctx: Guard, from_: Annotated[date, Query(alias="from")], to: date,
               group_by: Literal["day", "fleet", "powertrain"] = "day"):
    return CostReport.model_validate(await svc.cost_report(ctx, from_, to, group_by))


@router.post("/v1/reports/batch", response_model=BatchReportResult, summary="Run a PREDEFINED DuckDB analytics report over the lake")
async def batch(body: BatchReportRequest, ctx: Guard):
    return BatchReportResult.model_validate(await svc.batch_report(ctx, body.report_id, body.params))
