"""Ops: liveness, readiness, metrics."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ...errors import problem_response

router = APIRouter(tags=["Ops"])
PROBE_TIMEOUT_S = 2.0


@router.get("/healthz", summary="Liveness")
async def healthz() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/readyz", summary="Readiness (checks Postgres, Redis, Kafka; reports consumer lag)")
async def readyz(request: Request):
    deps = request.app.state.deps

    async def run(name, fn):
        try:
            detail = await asyncio.wait_for(fn(), PROBE_TIMEOUT_S)
            return name, {"ok": True, "detail": detail if isinstance(detail, (dict, str, int, float)) else None}
        except Exception as e:
            return name, {"ok": False, "detail": type(e).__name__}

    results = dict(await asyncio.gather(*(run(n, f) for n, f in deps.probes.items())))
    breakers = {n: b.state for n, b in deps.breakers.items()}
    if not all(r["ok"] for r in results.values()):
        failed = sorted(n for n, r in results.items() if not r["ok"])
        return problem_response(request, 503, "Service Unavailable", f"not ready: {', '.join(failed)}", "not-ready",
                                {"Retry-After": "5"}, {"checks": results, "breakers": breakers})
    return JSONResponse({"status": "ready", "checks": results, "breakers": breakers})


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    return Response(request.app.state.deps.metrics.render(), media_type="text/plain; version=0.0.4")
