"""Platform: OEM mapping lifecycle, data quality, DLQ."""
from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ...application import mappings as svc
from ...domain.roles import Perm
from ..context import Ctx, require
from ..idempotency import run_idempotent
from ..models import (
    DlqItem,
    DlqReplayRequest,
    DlqReplayResult,
    MappingDraft,
    MappingPreviewRequest,
    MappingPreviewResult,
    MappingSummary,
    OemQuality,
)
from ._common import PROBLEM_RESPONSES, IdemH, LimitQ, OemCode, Version, dump

router = APIRouter(tags=["Platform"], responses=PROBLEM_RESPONSES)
Read = Annotated[Ctx, Depends(require(Perm.PLATFORM_READ))]
Write = Annotated[Ctx, Depends(require(Perm.MAPPING_WRITE))]
Approve = Annotated[Ctx, Depends(require(Perm.MAPPING_APPROVE))]


@router.get("/v1/mappings", response_model=list[MappingSummary], summary="OEM mapping versions and status")
async def list_mappings(ctx: Read):
    return dump(MappingSummary, await svc.list_mappings(ctx))


@router.post("/v1/mappings", response_model=MappingSummary, status_code=201, summary="Create a DRAFT mapping (validated against the DSL)",
             responses={422: {"description": "invalid mapping", "content": {"application/problem+json": {}}}})
async def create_mapping(body: MappingDraft, ctx: Write):
    return MappingSummary.model_validate(await svc.create_draft(ctx, body.oem, body.document))


@router.post("/v1/mappings/preview", response_model=MappingPreviewResult, summary="Dry-run a draft mapping against a sample payload")
async def preview_mapping(body: MappingPreviewRequest, ctx: Write):
    ctx.note_resources("mapping_preview")
    return MappingPreviewResult.model_validate(await asyncio.to_thread(svc.preview, body.document, body.sample))


@router.post("/v1/mappings/{oem}/versions/{version}/approve", response_model=MappingSummary)
async def approve_mapping(oem: OemCode, version: Version, ctx: Approve, idempotency_key: IdemH = None):
    async def run():
        return 200, dump(MappingSummary, await svc.approve(ctx, oem, version))

    return await run_idempotent(ctx, None, run)


@router.post("/v1/mappings/{oem}/versions/{version}/activate", response_model=MappingSummary,
             summary="Activate (publishes to config.mappings.v1; normalizers hot-reload)")
async def activate_mapping(oem: OemCode, version: Version, ctx: Approve, idempotency_key: IdemH = None):
    async def run():
        return 200, dump(MappingSummary, await svc.activate(ctx, oem, version))

    return await run_idempotent(ctx, None, run)


@router.get("/v1/oems/quality", response_model=list[OemQuality], summary="Per-OEM data quality, drift and software-version fault correlation")
async def oem_quality(ctx: Read):
    ctx.note_resources("oem_quality")
    rows = await ctx.deps.quality.oem_quality()
    async with ctx.tx() as repos:
        active = await repos.mappings.active_versions()
    for r in rows:
        r["active_mapping_version"] = active.get(r.get("oem"))
    return dump(OemQuality, rows)


@router.get("/v1/dlq", response_model=list[DlqItem], summary="Dead-letter sample (original payload + error reason)")
async def dlq(ctx: Read, oem: Annotated[str | None, Query(pattern=r"^OEM_[A-Z]$")] = None,
              reason: Annotated[str | None, Query(max_length=64)] = None, limit: LimitQ = None):
    ctx.note_resources("dlq")
    return dump(DlqItem, await ctx.deps.quality.dlq_sample(oem, reason, ctx.limit(limit)))


@router.post("/v1/dlq/replay", response_model=DlqReplayResult, status_code=202, summary="Replay an OEM DLQ through its (now active) mapping")
async def dlq_replay(body: DlqReplayRequest, ctx: Approve, idempotency_key: IdemH = None):
    async def run():
        out = await ctx.deps.quality.replay_dlq(body.oem, body.reason, body.limit)
        return 202, dump(DlqReplayResult, out)

    return await run_idempotent(ctx, body.model_dump(mode="json"), run)
