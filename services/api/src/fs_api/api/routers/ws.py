"""WebSocket /v1/stream. Auth message FIRST (the token is never accepted in the URL), then subscribe; 1 Hz deltas."""
from __future__ import annotations

import asyncio
import contextlib
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ...application.stream import StreamSession
from ...domain.audit import AuditRecord
from ...domain.ratelimit import Limit
from ...domain.roles import Perm, can, purpose_allowed
from ...errors import ProblemError
from ..context import parse_purpose

router = APIRouter()
CLOSE_POLICY, CLOSE_UNAUTH, CLOSE_FORBIDDEN, CLOSE_BAD_REQUEST, CLOSE_RATE = 1008, 4401, 4403, 4400, 4429


async def _deny(ws: WebSocket, deps, code: int, reason: str, principal=None) -> None:
    deps.metrics.denied.labels(reason).inc()
    await deps.audit.publish(AuditRecord(
        action="WS /v1/stream", decision="DENY", tenant_id=principal.tenant_id if principal else "",
        actor_id=principal.sub if principal else "anonymous", resource_type="stream", reason=reason))
    with contextlib.suppress(Exception):
        await ws.send_json({"type": "error", "code": code, "detail": reason})
        await ws.close(code=code)


@router.websocket("/v1/stream")
async def stream(ws: WebSocket) -> None:
    deps = ws.app.state.deps
    s = deps.settings
    await ws.accept()
    if "token" in ws.query_params or "access_token" in ws.query_params:
        await _deny(ws, deps, CLOSE_BAD_REQUEST, "token-in-url")  # never let a JWT reach access logs
        return
    try:
        first = await asyncio.wait_for(ws.receive_json(), s.ws_auth_timeout_s)
    except (TimeoutError, ValueError, WebSocketDisconnect, KeyError):
        await _deny(ws, deps, CLOSE_UNAUTH, "no-auth-message")
        return
    if not isinstance(first, dict) or first.get("type") != "auth" or not isinstance(first.get("token"), str):
        await _deny(ws, deps, CLOSE_UNAUTH, "first-message-must-be-auth")
        return
    try:
        principal = deps.validator.validate(first["token"])
    except ProblemError as e:
        await _deny(ws, deps, CLOSE_UNAUTH, e.audit_reason)
        return
    try:
        purpose = parse_purpose(ws.headers.get("x-purpose"))
    except ProblemError:
        await _deny(ws, deps, CLOSE_BAD_REQUEST, "invalid-purpose", principal)
        return
    if not can(principal.roles, Perm.STREAM):
        await _deny(ws, deps, CLOSE_FORBIDDEN, "role", principal)
        return
    if not purpose_allowed(principal.roles, purpose):
        await _deny(ws, deps, CLOSE_FORBIDDEN, "purpose-denied", principal)
        return
    ok, _ = await deps.limiter.check([Limit("tenant", f"rl:t:{principal.tenant_id}", s.rate_tenant_per_min),
                                      Limit("user", f"rl:u:{principal.tenant_id}:{principal.sub}", s.rate_user_per_min)])
    if not ok:
        await _deny(ws, deps, CLOSE_RATE, "rate-limited", principal)
        return

    session = StreamSession(deps, principal, purpose)
    deps.metrics.ws_connections.inc()
    t0 = time.perf_counter()
    sent = 0
    inbox: asyncio.Queue = asyncio.Queue()
    feed_cm, feed = None, None

    async def reader() -> None:
        while True:
            try:
                await inbox.put(await ws.receive_json())
            except WebSocketDisconnect:
                await inbox.put(None)
                return
            except Exception:  # malformed JSON / binary frame
                await inbox.put({"__bad__": True})

    reader_task = asyncio.create_task(reader())
    reason = "client-closed"
    try:
        await ws.send_json({"type": "ready", "precision": session.precision(), "tick_ms": int(s.ws_tick_s * 1000)})
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while True:
            try:
                msg = await asyncio.wait_for(inbox.get(), max(0.0, next_tick - loop.time()))
            except TimeoutError:
                msg = ...  # sentinel: tick due
            if msg is None:
                break
            if msg is not ...:
                reply = session.handle(msg if not (isinstance(msg, dict) and msg.get("__bad__")) else None)
                if reply:
                    await ws.send_json(reply)
                    sent += 1
                if session.errors >= s.ws_max_bad_messages:
                    reason = "too-many-bad-messages"
                    await ws.close(code=CLOSE_POLICY)
                    break
                if reply and reply.get("type") == "subscribed":
                    next_tick = loop.time()  # push the snapshot immediately
                    if session.wants_alerts and feed is None:
                        feed_cm = deps.alert_feed.subscribe(principal.tenant_id)
                        feed = await feed_cm.__aenter__()
                continue
            if principal.exp and time.time() >= principal.exp:
                reason = "token-expired"
                await _send_close(ws, CLOSE_UNAUTH, "token expired")
                break
            for out in await session.tick(int(time.time() * 1000)):
                await ws.send_json(out)
                sent += 1
            if feed is not None:
                for _ in range(50):
                    alert = await feed.get(0.0)
                    if alert is None:
                        break
                    await ws.send_json({"type": "alert", "alert": alert})
                    sent += 1
            next_tick = loop.time() + s.ws_tick_s
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        reader_task.cancel()
        with contextlib.suppress(BaseException):
            await reader_task
        if feed_cm is not None:
            with contextlib.suppress(Exception):
                await feed_cm.__aexit__(None, None, None)
        deps.metrics.ws_connections.dec()
        await deps.audit.publish(AuditRecord(
            action="WS /v1/stream", decision="ALLOW", tenant_id=principal.tenant_id, actor_id=principal.sub, resource_type="stream",
            purpose=purpose.value, reason=reason, latency_ms=int((time.perf_counter() - t0) * 1000),
            attributes={"messages_sent": str(sent), "topics": ",".join(sorted(session.topics))}))


async def _send_close(ws: WebSocket, code: int, detail: str) -> None:
    with contextlib.suppress(Exception):
        await ws.send_json({"type": "error", "code": code, "detail": detail})
        await ws.close(code=code)
