"""Fleet: identity, KPIs, map snapshot."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ...application import vehicles as svc
from ...domain.roles import Perm, permissions_of
from ..context import Ctx, require
from ..models import FleetSummary, MapViewport, Me
from ._common import PROBLEM_RESPONSES, FleetQ

router = APIRouter(tags=["Fleet"], responses=PROBLEM_RESPONSES)


@router.get("/v1/me", response_model=Me, summary="Caller identity and effective permissions")
async def me(ctx: Annotated[Ctx, Depends(require(Perm.ME))]):
    ctx.note_resources("identity")
    perms = sorted(p.value for p in permissions_of(ctx.principal.roles))
    return Me(sub=ctx.principal.sub, tenant_id=ctx.tenant_id, roles=sorted(r.value for r in ctx.principal.roles), permissions=perms,
              email=ctx.principal.email)


@router.get("/v1/fleet/summary", response_model=FleetSummary, summary="Command-center KPIs")
async def fleet_summary(ctx: Annotated[Ctx, Depends(require(Perm.FLEET_READ))], fleet_id: FleetQ = None):
    ctx.note_resources("fleet_summary")
    return FleetSummary.model_validate(await svc.fleet_summary(ctx, str(fleet_id) if fleet_id else None))


@router.get("/v1/map/viewport", response_model=MapViewport, summary="Snapshot of vehicles in a viewport")
async def map_viewport(ctx: Annotated[Ctx, Depends(require(Perm.FLEET_READ))], bbox: Annotated[str, Query(examples=["77.4,12.8,77.8,13.2"])],
                       zoom: Annotated[int, Query(ge=1, le=20)]):
    return MapViewport.model_validate(await svc.map_viewport(ctx, bbox, zoom))
