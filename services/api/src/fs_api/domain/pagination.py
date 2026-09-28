"""Opaque, signed keyset cursors. The cursor binds the endpoint kind AND the tenant, so it cannot be replayed elsewhere."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


class BadCursor(ValueError):
    pass


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def encode_cursor(secret: str, kind: str, tenant_id: str, values: list[Any]) -> str:
    body = _b64(json.dumps({"k": kind, "t": tenant_id, "v": values}, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()[:16]
    return f"{body}.{_b64(sig)}"


def decode_cursor(secret: str, kind: str, tenant_id: str, cursor: str) -> list[Any]:
    try:
        body, sig = cursor.split(".", 1)
        expect = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(expect, _unb64(sig)):
            raise BadCursor("cursor signature mismatch")
        payload = json.loads(_unb64(body))
    except BadCursor:
        raise
    except Exception as e:  # malformed base64/json/shape
        raise BadCursor("malformed cursor") from e
    if payload.get("k") != kind or payload.get("t") != tenant_id or not isinstance(payload.get("v"), list):
        raise BadCursor("cursor does not belong to this endpoint or tenant")
    return payload["v"]


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


def build_page(rows: list[Any], limit: int, cursor_of) -> tuple[list[Any], str | None]:
    """Repos fetch limit+1 rows: if there is an extra row there is a next page keyed on the last returned row."""
    if len(rows) > limit:
        items = rows[:limit]
        return items, cursor_of(items[-1])
    return rows, None
