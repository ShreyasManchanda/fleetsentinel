"""Idempotency-Key handling for state-changing POSTs. First call runs and stores the response; a replay (same key + same
payload) returns the ORIGINAL status/body without re-executing; same key + different payload -> 422; concurrent -> 409."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi.responses import JSONResponse

from ..errors import ProblemError, bad_request
from .context import Ctx


def _fingerprint(ctx: Ctx, body: Any) -> str:
    raw = json.dumps({"p": ctx.request.path_params, "b": body}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


async def run_idempotent(
    ctx: Ctx,
    body: Any,
    fn: Callable[[], Awaitable[tuple[int, dict[str, Any]]]],
    redact: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> JSONResponse:
    key = ctx.request.headers.get("idempotency-key")
    if not key or not (8 <= len(key) <= 128):
        raise bad_request("Idempotency-Key header (8-128 chars) is required on this operation", "idempotency-key-required")
    route = ctx.request.scope.get("route")
    template = getattr(route, "path", ctx.request.url.path)
    scope = f"idem:{ctx.tenant_id}:{ctx.principal.sub}:{ctx.request.method}:{template}:{key}"
    ttl = ctx.deps.settings.idempotency_ttl_s
    state, record = await ctx.deps.idem.begin(scope, _fingerprint(ctx, body), ttl)
    if state == "replay" and record is not None:
        ctx.deps.metrics.idempotent_replays.inc()
        return JSONResponse(record["body"], status_code=record["status"], headers={"Idempotent-Replay": "true"})
    if state == "in_flight":
        raise ProblemError(409, "Conflict", "a request with this Idempotency-Key is still being processed", "idempotency-in-flight",
                           {"Retry-After": "1"})
    if state == "mismatch":
        raise ProblemError(422, "Unprocessable Content", "Idempotency-Key was already used with a different request payload",
                           "idempotency-key-reused")
    try:
        status, payload = await fn()
    except BaseException:
        await ctx.deps.idem.abort(scope)  # let the client retry with the same key
        raise
    stored = redact(payload) if redact else payload
    await ctx.deps.idem.complete(scope, {"status": status, "body": stored}, ttl)
    return JSONResponse(payload, status_code=status)
