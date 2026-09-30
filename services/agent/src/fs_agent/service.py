"""fs_agent HTTP service: chat + briefing for the API's copilot proxy.

Tenant arrives via X-Tenant-ID (the API already authenticated the bearer); the service
never trusts tenant claims from the LLM layer.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from .briefing import build_briefing, opportunities_from_inputs
from .graph import ask
from .tools import ToolRegistry

app = FastAPI(title="Sentinel Copilot")


class ChatBody(BaseModel):
    message: str
    conversation_id: str | None = None


def _registry() -> ToolRegistry:
    # Live backends call the API with the tenant's bearer token; until wired, tools
    # raise UnknownTool -> the graph degrades to a templated answer (never a crash).
    return ToolRegistry({})


def _stub_llm(question: str, tools: list, history: list) -> dict:
    return {"answer": "Copilot LLM is not configured; the rules briefing is available at /briefing."}


@app.post("/chat")
def chat(body: ChatBody, x_tenant_id: str = Header(default="unknown")):
    if not body.message.strip():
        raise HTTPException(422, "empty message")
    out = ask(_registry(), _stub_llm, x_tenant_id, body.message)
    return {"conversation_id": body.conversation_id or str(uuid.uuid4()),
            "answer": out["answer"], "grounded": out["grounded"],
            "tool_calls": out["tool_calls"],
            "proposed_actions": None, "degraded": out["degraded"] or True}


@app.get("/briefing")
def get_briefing(x_tenant_id: str = Header(default="unknown")):
    return build_briefing([], datetime.now(UTC).isoformat())


@app.post("/briefing")
def post_briefing(body: dict, x_tenant_id: str = Header(default="unknown")):
    opps = opportunities_from_inputs(
        idle_leakage_usd=float(body.get("idle_leakage_usd", 0.0)),
        idle_detail=str(body.get("idle_detail", "")),
        risks=body.get("risks", []),
        safety_flags=body.get("safety_flags", []))
    return build_briefing(opps, datetime.now(UTC).isoformat())


@app.get("/health")
def health():
    return {"ok": True, "llm": bool(os.getenv("FS_LLM_API_KEY"))}
