"""OEM mapping lifecycle: draft -> approved -> active (publishes to config.mappings.v1). Mappings are DATA, validated by the
DSL compiler in fscore.mapping_engine; nothing here executes user code."""
from __future__ import annotations

from typing import Any

from fscore.mapping_engine import Mapping, MappingError

from ..api.context import Ctx
from ..errors import conflict, forbidden, not_found, unprocessable

MAX_PREVIEW_RECORDS = 200


def validate_document(document: dict[str, Any], oem: str) -> Mapping:
    if document.get("oem") not in (None, oem):
        raise unprocessable(f"document.oem ({document.get('oem')}) does not match oem ({oem})", "mapping-oem-mismatch")
    doc = dict(document)
    doc["oem"] = oem
    doc.setdefault("version", 1)
    try:
        return Mapping(doc)
    except MappingError as e:
        raise unprocessable(f"invalid mapping document: {e}", "mapping-invalid") from e
    except (TypeError, KeyError, ValueError, AttributeError) as e:
        raise unprocessable(f"invalid mapping document: {type(e).__name__}: {e}", "mapping-invalid") from e


def summary_out(row: dict[str, Any]) -> dict[str, Any]:
    return {"oem": row["oem"], "version": row["version"], "status": row["status"], "created_by": row.get("created_by"),
            "created_at": row.get("created_at"), "activated_at": row.get("activated_at")}


async def list_mappings(ctx: Ctx) -> list[dict[str, Any]]:
    async with ctx.tx() as repos:
        rows = await repos.mappings.list_all()
    return [summary_out(r) for r in rows]


async def create_draft(ctx: Ctx, oem: str, document: dict[str, Any]) -> dict[str, Any]:
    validate_document(document, oem)
    async with ctx.tx() as repos:
        version = await repos.mappings.next_version(oem)
        if version is None:
            raise unprocessable(f"unknown OEM {oem}", "unknown-oem")
        doc = dict(document)
        doc["oem"], doc["version"] = oem, version  # the server owns the version number (unique per OEM, monotonically increasing)
        row = await repos.mappings.create_draft(oem, version, doc, ctx.principal.sub)
    if row is None:
        raise conflict("mapping version was created concurrently, retry")
    ctx.note_resources("mapping", [f"{oem}:{version}"])
    return summary_out(row)


def preview(document: dict[str, Any], sample: Any) -> dict[str, Any]:
    oem = str(document.get("oem") or "OEM_X")
    mapping = validate_document(document, oem)
    result = mapping.apply(sample)
    ok = result.ok[:MAX_PREVIEW_RECORDS]
    failed = [{"reason": f.reason, "detail": f.detail, "raw": f.raw} for f in result.failed[:MAX_PREVIEW_RECORDS]]
    return {"ok": ok, "failed": failed}


async def approve(ctx: Ctx, oem: str, version: int) -> dict[str, Any]:
    async with ctx.tx() as repos:
        row = await repos.mappings.get(oem, version)
        if row is None:
            raise not_found("mapping version")
        if row["status"] in ("approved", "active"):
            return summary_out(row)
        if row["status"] != "draft":
            raise conflict(f"mapping is {row['status']}, only drafts can be approved")
        if ctx.deps.settings.four_eyes_mapping_approval and row.get("created_by") == ctx.principal.sub:
            raise forbidden("the author of a mapping cannot approve it (four-eyes)", "four-eyes")
        row = await repos.mappings.set_status(oem, version, "approved")
    return summary_out(row)


async def activate(ctx: Ctx, oem: str, version: int) -> dict[str, Any]:
    """approved -> active. The DB change commits FIRST, then the document is published; if publishing fails the client retries
    with the same Idempotency-Key and the (already active) version is simply re-published."""
    async with ctx.tx() as repos:
        row = await repos.mappings.get(oem, version)
        if row is None:
            raise not_found("mapping version")
        if row["status"] not in ("approved", "active"):
            raise conflict(f"mapping is {row['status']}, it must be approved before activation")
        if row["status"] != "active":
            row = await repos.mappings.activate(oem, version)
    await ctx.deps.mapping_pub.publish(oem, row["document"])
    ctx.note_resources("mapping", [f"{oem}:{version}"])
    return summary_out(row)
