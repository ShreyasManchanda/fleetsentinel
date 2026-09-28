"""Agent: copilot proxy, briefing, and the human-approval action queue."""
from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from ...application import actions as svc
from ...domain.roles import Perm
from ...errors import unavailable
from ...ports import AgentUnavailable
from ..context import Ctx, bearer_token, require
from ..idempotency import run_idempotent
from ..models import ActionPage, ActionProposalCreate, AgentAction, AgentAnswer, AgentChatRequest, Briefing, RejectBody
from ._common import PROBLEM_RESPONSES, CursorQ, IdemH, LimitQ, dump

router = APIRouter(tags=["Agent"], responses=PROBLEM_RESPONSES)
Chat = Annotated[Ctx, Depends(require(Perm.AGENT_CHAT))]
Read = Annotated[Ctx, Depends(require(Perm.AGENT_READ))]
Write = Annotated[Ctx, Depends(require(Perm.AGENT_WRITE))]
Approve = Annotated[Ctx, Depends(require(Perm.AGENT_APPROVE))]


def _copilot_down(e: Exception):
    return unavailable("the copilot is unavailable; the rest of the platform is unaffected", 10, "copilot-unavailable")


@router.post("/v1/agent/chat", response_model=AgentAnswer, summary="Ask the copilot",
             responses={503: {"description": "copilot unavailable", "content": {"application/problem+json": {}}}})
async def chat(body: AgentChatRequest, ctx: Chat):
    ctx.note_resources("agent_chat")
    try:
        out = await ctx.deps.agent.chat(bearer_token(ctx.request), ctx.tenant_id, body.message,
                                        str(body.conversation_id) if body.conversation_id else None)
    except AgentUnavailable as e:
        raise _copilot_down(e) from e
    return AgentAnswer.model_validate(out)


@router.get("/v1/agent/briefing", response_model=Briefing, summary="Latest daily briefing")
async def get_briefing(ctx: Read):
    try:
        out = await ctx.deps.agent.briefing(bearer_token(ctx.request), ctx.tenant_id, False)
    except AgentUnavailable as e:
        raise _copilot_down(e) from e
    return Briefing.model_validate(out)


@router.post("/v1/agent/briefing", response_model=Briefing, status_code=202, summary="Generate a fresh briefing now")
async def post_briefing(ctx: Write, idempotency_key: IdemH = None):
    async def run():
        try:
            out = await ctx.deps.agent.briefing(bearer_token(ctx.request), ctx.tenant_id, True)
        except AgentUnavailable as e:
            raise _copilot_down(e) from e
        return 202, dump(Briefing, out)

    return await run_idempotent(ctx, None, run)


@router.get("/v1/agent/actions", response_model=ActionPage, summary="Action proposals (approval queue)")
async def list_actions(ctx: Read, status: Annotated[Literal["pending", "approved", "rejected"] | None, Query()] = None,
                       cursor: CursorQ = None, limit: LimitQ = None):
    return ActionPage.model_validate(await svc.list_actions(ctx, status, cursor, limit))


@router.post("/v1/agent/actions", response_model=AgentAction, status_code=201, summary="Create a PENDING proposal")
async def create_action(body: ActionProposalCreate, ctx: Write, idempotency_key: IdemH = None):
    async def run():
        row = await svc.create_action(ctx, proposal=body.proposal, rationale=body.rationale, expected_impact_usd=body.expected_impact_usd,
                                      alert_id=str(body.alert_id) if body.alert_id else None, idempotency_key=ctx.request.headers["idempotency-key"])
        return 201, dump(AgentAction, row)

    return await run_idempotent(ctx, body.model_dump(mode="json"), run)


@router.post("/v1/agent/actions/{id}/approve", response_model=AgentAction, summary="Approve a pending proposal -> work order (fleet_admin only)",
             responses={409: {"description": "not pending", "content": {"application/problem+json": {}}}})
async def approve(id: UUID, ctx: Approve, idempotency_key: IdemH = None):
    async def run():
        return 200, dump(AgentAction, await svc.approve(ctx, str(id)))

    return await run_idempotent(ctx, None, run)


@router.post("/v1/agent/actions/{id}/reject", response_model=AgentAction)
async def reject(id: UUID, ctx: Approve, body: RejectBody | None = None, idempotency_key: IdemH = None):
    async def run():
        return 200, dump(AgentAction, await svc.reject(ctx, str(id), body.reason if body else None))

    return await run_idempotent(ctx, body.model_dump(mode="json") if body else None, run)
