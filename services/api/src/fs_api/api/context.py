"""Per-request context and the deny-by-default guard. Order: authenticate (401) -> rate limit (429) -> RBAC (403) -> purpose (403)."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from fastapi import Header, Request

from ..container import Deps
from ..domain.pagination import BadCursor, clamp_limit, decode_cursor, encode_cursor
from ..domain.ratelimit import Limit, rate_headers
from ..domain.roles import Perm, Purpose, can, location_precision, purpose_allowed
from ..errors import ProblemError, bad_request, forbidden, unauthorized, unprocessable
from ..ports import Repos
from ..security.jwt_auth import Principal

COPILOT_PATHS = ("/v1/agent/chat",)


def bearer_token(request: Request) -> str:
    h = request.headers.get("authorization", "")
    scheme, _, token = h.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise unauthorized("missing bearer token")
    return token.strip()


def parse_purpose(raw: str | None) -> Purpose:
    if raw is None or raw.strip() == "":
        return Purpose.OPERATIONS
    try:
        return Purpose(raw.strip().upper())
    except ValueError:
        raise unprocessable("X-Purpose must be one of OPERATIONS, RECOVERY, SAFETY_REVIEW, COMPLIANCE", "invalid-purpose") from None


@dataclass(slots=True)
class Ctx:
    request: Request
    deps: Deps
    principal: Principal
    purpose: Purpose

    @property
    def tenant_id(self) -> str:
        return self.principal.tenant_id

    def tx(self) -> AbstractAsyncContextManager[Repos]:
        return self.deps.db.tx(self.principal.tenant_id)

    def precision(self, flagged: bool = False) -> str:
        return location_precision(self.principal.roles, self.purpose, flagged)

    def can(self, perm: Perm) -> bool:
        return can(self.principal.roles, perm)

    def cursor_in(self, kind: str, cursor: str | None) -> list[Any] | None:
        if not cursor:
            return None
        try:
            return decode_cursor(self.deps.settings.cursor_secret, kind, self.tenant_id, cursor)
        except BadCursor as e:
            raise bad_request(str(e), "bad-cursor") from e

    def cursor_out(self, kind: str, values: list[Any]) -> str:
        return encode_cursor(self.deps.settings.cursor_secret, kind, self.tenant_id, values)

    @staticmethod
    def limit(limit: int | None) -> int:
        return clamp_limit(limit)

    def note_resources(self, resource_type: str | None = None, ids: list[str] | None = None) -> None:
        st = self.request.state
        if resource_type:
            st.audit_resource_type = resource_type
        if ids:
            st.audit_resource_ids = list(getattr(st, "audit_resource_ids", [])) + [str(i) for i in ids]


async def authenticate(request: Request, deps: Deps) -> Principal:
    try:
        p = deps.validator.validate(bearer_token(request))
    except ProblemError as e:
        deps.metrics.denied.labels(e.audit_reason).inc()
        raise
    request.state.principal = p
    return p


async def enforce_rate_limit(request: Request, deps: Deps, p: Principal) -> None:
    s = deps.settings
    limits = [Limit("tenant", f"rl:t:{p.tenant_id}", s.rate_tenant_per_min), Limit("user", f"rl:u:{p.tenant_id}:{p.sub}", s.rate_user_per_min)]
    if request.url.path in COPILOT_PATHS:
        limits.append(Limit("copilot", f"rl:c:{p.tenant_id}:{p.sub}", s.rate_copilot_per_min))
    ok, decision = await deps.limiter.check(limits)
    request.state.rate_headers = rate_headers(decision)
    if not ok:
        deps.metrics.rate_limited.labels("request").inc()
        raise ProblemError(429, "Too Many Requests", "rate limit exceeded", "rate-limited", rate_headers(decision),
                           audit_reason="rate-limited")


def require(perm: Perm) -> Callable[..., Any]:
    """Route guard factory. Every route MUST use it (a test asserts that no route is unguarded)."""

    async def guard(request: Request, x_purpose: str | None = Header(None, alias="X-Purpose")) -> Ctx:
        deps: Deps = request.app.state.deps
        principal = await authenticate(request, deps)
        await enforce_rate_limit(request, deps, principal)
        if not can(principal.roles, perm):
            deps.metrics.denied.labels("role").inc()
            raise forbidden(f"role not permitted for {perm.value}", "role")
        purpose = parse_purpose(x_purpose)
        request.state.purpose = purpose.value
        if not purpose_allowed(principal.roles, purpose):
            deps.metrics.denied.labels("purpose").inc()
            raise forbidden("purpose RECOVERY is reserved for lender_agent", "purpose-denied")
        return Ctx(request, deps, principal, purpose)

    guard.required_perm = perm  # type: ignore[attr-defined]
    return guard
