"""RFC 9457 problem+json errors."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_CT = "application/problem+json"
TYPE_BASE = "https://fleetsentinel.dev/problems/"


class ProblemError(Exception):
    """Raise anywhere; rendered as application/problem+json."""

    def __init__(
        self,
        status: int,
        title: str,
        detail: str | None = None,
        code: str = "about:blank",
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
        audit_reason: str | None = None,
    ):
        super().__init__(f"{status} {title}: {detail}")
        self.status = status
        self.title = title
        self.detail = detail
        self.code = code
        self.headers = headers or {}
        self.extra = extra or {}
        self.audit_reason = audit_reason or code


def unauthorized(detail: str = "missing or invalid bearer token") -> ProblemError:
    return ProblemError(401, "Unauthorized", detail, "unauthorized", {"WWW-Authenticate": "Bearer"}, audit_reason="unauthenticated")


def forbidden(detail: str = "insufficient role", reason: str = "forbidden") -> ProblemError:
    return ProblemError(403, "Forbidden", detail, reason, audit_reason=reason)


def not_found(what: str = "resource") -> ProblemError:
    return ProblemError(404, "Not Found", f"{what} not found", "not-found")


def conflict(detail: str, code: str = "conflict") -> ProblemError:
    return ProblemError(409, "Conflict", detail, code)


def bad_request(detail: str, code: str = "bad-request") -> ProblemError:
    return ProblemError(400, "Bad Request", detail, code)


def unprocessable(detail: str, code: str = "unprocessable") -> ProblemError:
    return ProblemError(422, "Unprocessable Content", detail, code)


def unavailable(detail: str, retry_after_s: int = 5, code: str = "unavailable") -> ProblemError:
    return ProblemError(503, "Service Unavailable", detail, code, {"Retry-After": str(max(1, int(retry_after_s)))})


def trace_id_of(request: Request) -> str:
    tp = request.headers.get("traceparent", "")
    parts = tp.split("-")
    if len(parts) == 4 and len(parts[1]) == 32:
        return parts[1]
    existing = getattr(request.state, "trace_id", None)
    if existing:
        return existing
    tid = uuid.uuid4().hex
    request.state.trace_id = tid
    return tid


def problem_response(request: Request, status: int, title: str, detail: str | None, code: str,
                     headers: dict[str, str] | None = None, extra: dict[str, Any] | None = None) -> JSONResponse:
    body: dict[str, Any] = {
        "type": code if code.startswith("http") or code == "about:blank" else TYPE_BASE + code,
        "title": title,
        "status": status,
        "instance": request.url.path,
        "trace_id": trace_id_of(request),
    }
    if detail:
        body["detail"] = detail
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status, headers=headers, media_type=PROBLEM_CT)


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem(request: Request, exc: ProblemError):
        request.state.audit_reason = exc.audit_reason
        return problem_response(request, exc.status, exc.title, exc.detail, exc.code, exc.headers, exc.extra)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        titles = {404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large"}
        return problem_response(request, exc.status_code, titles.get(exc.status_code, "Error"),
                                str(exc.detail) if exc.detail else None, "http-" + str(exc.status_code),
                                dict(exc.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errs = [{"loc": ".".join(str(x) for x in e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return problem_response(request, 422, "Unprocessable Content", "request validation failed", "validation",
                                extra={"errors": errs})
