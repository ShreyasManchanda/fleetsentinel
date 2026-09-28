"""FastAPI application factory. Everything I/O comes in through `Deps`, so tests build the full app with in-memory fakes."""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from ..container import Deps
from ..errors import install_handlers
from .middleware import CoreMiddleware
from .routers import agent, alerts, compliance, drivers, fleet, ops, platform, reports, vehicles, ws

OPEN_PATHS = {"/healthz", "/readyz"}
DESCRIPTION = (
    "Connected-vehicle intelligence API. Implements contracts/openapi.yaml. OIDC bearer JWT (RS256), tenant from the tenant_id claim "
    "only, Postgres RLS, deny-by-default RBAC, RFC 9457 errors, keyset pagination, rate limits, Idempotency-Key, audit of every request."
)


def create_app(deps: Deps) -> FastAPI:
    s = deps.settings
    dev = s.env == "dev"
    app = FastAPI(
        title="FleetSentinel API", version="1.0.0", description=DESCRIPTION, redirect_slashes=False,
        docs_url="/docs" if dev else None, redoc_url=None, openapi_url="/openapi.json" if dev else None,
    )
    app.state.deps = deps
    install_handlers(app)
    for r in (ops, fleet, vehicles, alerts, drivers, reports, agent, platform, compliance):
        app.include_router(r.router)
    app.include_router(ws.router)

    # middleware order: last added = outermost. CORS handles preflight before the audit/limits layer sees the request.
    app.add_middleware(CoreMiddleware)
    app.add_middleware(
        CORSMiddleware, allow_origins=s.cors_origins, allow_credentials=False, allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Purpose"],
        expose_headers=["RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset", "Retry-After", "X-Trace-Id", "Idempotent-Replay"],
        max_age=600,
    )

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, description=DESCRIPTION, routes=app.routes,
                             servers=[{"url": "http://localhost:8000", "description": "local compose / kind"}])
        schema.setdefault("components", {})["securitySchemes"] = {
            "oidc": {"type": "openIdConnect", "openIdConnectUrl": f"{s.oidc_issuer}/.well-known/openid-configuration"}}
        for path, item in schema["paths"].items():
            for op in item.values():
                op["security"] = [] if path in OPEN_PATHS else [{"oidc": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app
