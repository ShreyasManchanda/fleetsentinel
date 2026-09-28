"""Pure-ASGI core middleware: 1 MB body limit, security headers, trace id, metrics and the audit event for EVERY request."""
from __future__ import annotations

import time
from typing import Any

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..domain.audit import AuditRecord, resource_type_of
from ..domain.breaker import CircuitOpen
from ..errors import ProblemError, problem_response, trace_id_of
from ..infra_errors import is_infra_error

log = structlog.get_logger("fs_api.http")

SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
    "Cross-Origin-Resource-Policy": "same-site",
}
DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


class BodyTooLarge(Exception):
    pass


class CoreMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        deps = scope["app"].state.deps
        state: dict[str, Any] = scope.setdefault("state", {})
        t0 = time.perf_counter()
        limit = deps.settings.body_limit_bytes
        holder = {"status": 0, "started": False}
        request = Request(scope)
        is_doc = scope["path"].startswith(DOC_PATHS)

        async def limited_receive() -> Message:
            msg = await receive()
            if msg["type"] == "http.request":
                state["_body_bytes"] = state.get("_body_bytes", 0) + len(msg.get("body", b""))
                if state["_body_bytes"] > limit:
                    raise BodyTooLarge
            return msg

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                holder["status"] = message["status"]
                holder["started"] = True
                h = MutableHeaders(scope=message)
                for k, v in SECURITY_HEADERS.items():
                    if not (is_doc and k == "Content-Security-Policy"):
                        h.setdefault(k, v)
                h["X-Trace-Id"] = trace_id_of(request)
                for k, v in state.get("rate_headers", {}).items():
                    h.setdefault(k, v)
            await send(message)

        async def respond(resp) -> None:
            await resp(scope, receive, send_wrapper)

        cl = Headers(scope=scope).get("content-length")
        try:
            if cl and cl.isdigit() and int(cl) > limit:
                state["audit_reason"] = "body-too-large"
                await respond(problem_response(request, 413, "Payload Too Large", f"request body exceeds {limit} bytes", "payload-too-large"))
            else:
                await self.app(scope, limited_receive, send_wrapper)
        except BodyTooLarge:
            state["audit_reason"] = "body-too-large"
            if not holder["started"]:
                await respond(problem_response(request, 413, "Payload Too Large", f"request body exceeds {limit} bytes", "payload-too-large"))
        except Exception as exc:
            if holder["started"]:
                raise
            if isinstance(exc, CircuitOpen):
                resp = problem_response(request, 503, "Service Unavailable", f"dependency '{exc.name}' is temporarily unavailable",
                                        "dependency-open", {"Retry-After": str(max(1, int(exc.retry_after_s + 0.999)))})
                state["audit_reason"] = f"circuit-open:{exc.name}"
            elif isinstance(exc, ProblemError):
                resp = problem_response(request, exc.status, exc.title, exc.detail, exc.code, exc.headers, exc.extra)
                state["audit_reason"] = exc.audit_reason
            elif is_infra_error(exc):
                log.warning("dependency failure", error=type(exc).__name__, path=scope["path"])
                resp = problem_response(request, 503, "Service Unavailable", "a backing service is unavailable", "dependency-unavailable",
                                        {"Retry-After": "5"})
                state["audit_reason"] = f"dependency:{type(exc).__name__}"
            else:
                log.error("unhandled error", error=repr(exc), path=scope["path"])
                resp = problem_response(request, 500, "Internal Server Error", "unexpected error", "internal")
                state["audit_reason"] = "internal-error"
            await respond(resp)
        finally:
            await self._finish(deps, scope, state, holder["status"] or 500, t0)

    async def _finish(self, deps: Any, scope: Scope, state: dict[str, Any], status: int, t0: float) -> None:
        elapsed = time.perf_counter() - t0
        route = scope.get("route")
        template = getattr(route, "path", None) or "<unmatched>"
        method = scope["method"]
        deps.metrics.requests.labels(method, template, str(status)).inc()
        deps.metrics.latency.labels(method, template).observe(elapsed)
        if scope["path"] in deps.settings.audit_skip_paths or (method == "OPTIONS" and "access-control-request-method" in Headers(scope=scope)):
            return
        principal = state.get("principal")
        decision = "DENY" if status in (401, 403, 429) else "ALLOW"
        reason = state.get("audit_reason") or (f"status={status}" if status >= 400 else "")
        params = {k: str(v) for k, v in (scope.get("path_params") or {}).items()}
        ids = list(params.values()) + list(state.get("audit_resource_ids", []))
        rec = AuditRecord(
            action=f"{method} {template}",
            decision=decision,
            tenant_id=principal.tenant_id if principal else "",
            actor_id=principal.sub if principal else "anonymous",
            actor_type="USER",
            resource_type=state.get("audit_resource_type") or resource_type_of(template),
            resource_ids=ids,
            purpose=state.get("purpose", ""),
            reason=reason,
            latency_ms=int(elapsed * 1000),
            attributes={"status": str(status), "trace_id": state.get("trace_id", "")},
        )
        deps.metrics.audit_events.labels(decision).inc()
        try:
            await deps.audit.publish(rec)
        except Exception:  # the publisher is resilient; never fail a request because auditing hiccuped
            log.error("audit publish failed", action=rec.action)
        buffered = getattr(deps.audit, "buffered", None)
        if buffered is not None:
            deps.metrics.audit_buffered.set(buffered)
