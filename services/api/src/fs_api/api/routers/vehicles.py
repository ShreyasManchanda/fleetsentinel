"""Vehicles."""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from ...application import vehicles as svc
from ...domain.roles import Perm
from ..context import Ctx, require
from ..models import Lookalike, RiskDetail, TripPage, Vehicle360, VehiclePage, VehicleStatus
from ._common import PROBLEM_RESPONSES, CursorQ, FleetQ, LimitQ, Vin, dump

router = APIRouter(tags=["Vehicles"], responses=PROBLEM_RESPONSES)
Guard = Annotated[Ctx, Depends(require(Perm.FLEET_READ))]


@router.get("/v1/vehicles", response_model=VehiclePage, summary="List vehicles (keyset pagination; alert counts joined in ONE query)")
async def list_vehicles(ctx: Guard, cursor: CursorQ = None, limit: LimitQ = None, fleet_id: FleetQ = None,
                        status: Annotated[VehicleStatus | None, Query()] = None,
                        min_risk: Annotated[float | None, Query(ge=0, le=1)] = None,
                        vin_prefix: Annotated[str | None, Query(min_length=3, max_length=17, pattern=r"^[A-Za-z0-9]+$")] = None,
                        sort: Literal["risk_desc", "vin_asc"] = "risk_desc"):
    return VehiclePage.model_validate(await svc.list_vehicles(
        ctx, fleet_id=str(fleet_id) if fleet_id else None, status=status, min_risk=min_risk, vin_prefix=vin_prefix, sort=sort,
        cursor=cursor, limit=limit))


@router.get("/v1/vehicles/{vin}", response_model=Vehicle360, summary="Vehicle 360")
async def vehicle_360(vin: Vin, ctx: Guard):
    return Vehicle360.model_validate(await svc.vehicle_360(ctx, vin))


@router.get("/v1/vehicles/{vin}/trips", response_model=TripPage, summary="Trip history (keyset on (start_ts,id))")
async def vehicle_trips(vin: Vin, ctx: Guard, cursor: CursorQ = None, limit: LimitQ = None):
    return TripPage.model_validate(await svc.vehicle_trips(ctx, vin, cursor, limit))


@router.get("/v1/vehicles/{vin}/risk", response_model=RiskDetail, summary="Latest maintenance risk with reason codes")
async def vehicle_risk(vin: Vin, ctx: Guard):
    return RiskDetail.model_validate(await svc.vehicle_risk(ctx, vin))


@router.get("/v1/vehicles/{vin}/lookalikes", response_model=list[Lookalike], summary="Look-alike vehicles (pgvector kNN under RLS)")
async def lookalikes(vin: Vin, ctx: Guard, k: Annotated[int, Query(ge=1, le=20)] = 5):
    return dump(Lookalike, await svc.vehicle_lookalikes(ctx, vin, k))
