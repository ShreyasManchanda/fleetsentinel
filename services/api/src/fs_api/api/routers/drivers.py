"""Drivers."""
from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends

from ...application import drivers as svc
from ...domain.roles import Perm
from ..context import Ctx, require
from ..models import DriverDetail, DriverPage
from ._common import PROBLEM_RESPONSES, CursorQ, FleetQ, LimitQ

router = APIRouter(tags=["Drivers"], responses=PROBLEM_RESPONSES)
Guard = Annotated[Ctx, Depends(require(Perm.DRIVER_READ))]


@router.get("/v1/drivers", response_model=DriverPage, summary="Driver scorecards (PII decrypted only for authorised roles)")
async def list_drivers(ctx: Guard, cursor: CursorQ = None, limit: LimitQ = None, fleet_id: FleetQ = None,
                       sort: Literal["score_asc", "score_desc"] = "score_asc"):
    return DriverPage.model_validate(await svc.list_drivers(ctx, sort=sort, fleet_id=str(fleet_id) if fleet_id else None,
                                                            cursor=cursor, limit=limit))


@router.get("/v1/drivers/{id}", response_model=DriverDetail)
async def get_driver(id: UUID, ctx: Guard):
    return DriverDetail.model_validate(await svc.get_driver(ctx, str(id)))
