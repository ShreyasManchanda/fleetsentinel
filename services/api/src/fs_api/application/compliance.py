"""Compliance use cases: audit log + chain verification, data-subject requests (crypto-shredding erasure, access export)
and the privacy-safe insight export."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, date, datetime
from typing import Any

from fscore import crypto

from ..api.context import Ctx
from ..domain.audit import AuditRecord
from ..domain.privacy import k_anonymize
from ..errors import not_found, unprocessable

log = logging.getLogger("fs_api.compliance")
PROBE_PLAINTEXT = b"erasure-probe"
AAD_PROBE = b"dsr.probe"
MAX_EXPORT_DAYS = 366


# ---------------------------------------------------------------- audit
def audit_entry_out(r: dict[str, Any]) -> dict[str, Any]:
    h = r.get("hash")
    return {
        "seq": r["seq"], "ts": r["ts"], "actor_id": r.get("actor_id"), "actor_type": r.get("actor_type"), "action": r["action"],
        "resource_type": r.get("resource_type"), "resource_ids": list(r.get("resource_ids") or []), "purpose": r.get("purpose"),
        "decision": r.get("decision"), "reason": r.get("reason"), "hash": h.hex() if isinstance(h, (bytes, bytearray, memoryview)) else h,
    }


async def list_audit(ctx: Ctx, *, actor_id: str | None, action: str | None, decision: str | None, cursor: str | None,
                     limit: int | None) -> dict[str, Any]:
    n = ctx.limit(limit)
    after = ctx.cursor_in("audit", cursor)
    async with ctx.tx() as repos:
        rows = await repos.audit.list_page(actor_id=actor_id, action=action, decision=decision, after=after, limit=n + 1)
    nxt = None
    if len(rows) > n:
        rows = rows[:n]
        nxt = ctx.cursor_out("audit", [int(rows[-1]["seq"])])
    return {"items": [audit_entry_out(r) for r in rows], "next_cursor": nxt}


async def verify_audit(ctx: Ctx, from_seq: int, to_seq: int | None) -> dict[str, Any]:
    async with ctx.tx() as repos:
        r = await repos.audit.verify(from_seq, to_seq)
    h = r.get("head_hash")
    return {"ok": bool(r["ok"]), "checked": int(r["checked"]), "first_bad_seq": r.get("first_bad_seq"),
            "head_hash": h.hex() if isinstance(h, (bytes, bytearray, memoryview)) else h, "anchored_day": r.get("anchored_day")}


# ---------------------------------------------------------------- data-subject requests
def dsr_out(row: dict[str, Any], extra_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    ev = dict(row.get("evidence") or {})
    if extra_evidence:
        ev.update(extra_evidence)
    return {"id": row["id"], "kind": row["kind"], "status": row["status"], "requested_at": row["requested_at"],
            "completed_at": row.get("completed_at"), "evidence": ev}


def redact_dsr(payload: dict[str, Any]) -> dict[str, Any]:
    """Idempotency records live in Redis: never persist the exported PII there."""
    out = dict(payload)
    ev = dict(out.get("evidence") or {})
    if "export" in ev:
        ev["export"] = "[redacted: available only in the original response]"
    out["evidence"] = ev
    return out


def _sha(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data else None


async def get_dsr(ctx: Ctx, dsr_id: str) -> dict[str, Any]:
    async with ctx.tx() as repos:
        row = await repos.dsr.get(dsr_id)
    if row is None:
        raise not_found("data-subject request")
    ctx.note_resources("dsr", [dsr_id])
    return dsr_out(row)


async def create_dsr(ctx: Ctx, driver_id: str, kind: str) -> dict[str, Any]:
    async with ctx.tx() as repos:
        subject = await repos.dsr.load_subject(driver_id)
        if subject is None:
            raise not_found("driver")
        dsr = await repos.dsr.create(driver_id, kind)
        ctx.note_resources("dsr", [str(dsr["id"]), driver_id])
        if kind == "ERASE":
            return await _erase(ctx, repos, dsr, subject)
        return await _access(ctx, repos, dsr, subject)


async def _erase(ctx: Ctx, repos: Any, dsr: dict[str, Any], subject: dict[str, Any]) -> dict[str, Any]:
    """Crypto-shredding (ADR-04): destroy the subject key so EVERY ciphertext copy (Postgres, Kafka, lake, backups) is unreadable."""
    driver_id = str(subject["id"])
    key_id = subject["subject_key_id"]
    keys = ctx.deps.keys
    already = subject.get("erased_at") is not None or subject.get("wrapped_dek") is None
    captured: list[tuple[bytes, bytes]] = [(b, a) for b, a in ((subject.get("name_enc"), crypto.AAD_DRIVER_NAME), (subject.get("license_enc"), crypto.AAD_DRIVER_LICENSE)) if b]
    readable_before: bool | None = None
    if not already:
        # Capture ciphertext BEFORE destruction so the replay check is meaningful even for a driver with no stored PII.
        try:
            dek = await asyncio.to_thread(keys.dek, key_id, subject["wrapped_dek"])
            captured.append((crypto.encrypt(dek, key_id, PROBE_PLAINTEXT, AAD_PROBE), AAD_PROBE))
            if subject.get("name_enc"):
                await asyncio.to_thread(crypto.decrypt, dek, subject["name_enc"], crypto.AAD_DRIVER_NAME)
            readable_before = True
        except crypto.CryptoError:
            readable_before = False
        except Exception as e:  # Vault down: erasure must NOT depend on it (it is a DB-only destroy)
            log.warning("pre-erasure readability probe skipped: %s", type(e).__name__)

    destroyed = await repos.dsr.destroy_key(key_id) if not already else False
    nulled = await repos.dsr.null_pii(driver_id)
    trips = await repos.dsr.pseudonymize_trips(driver_id)
    keys.invalidate(key_id)  # drop any cached plaintext DEK in this process immediately
    purged, purge_errors = 0, []
    for name, store in (("redis", ctx.deps.state), ("mongo", ctx.deps.twins)):
        try:
            purged += await store.purge_driver(ctx.tenant_id, driver_id)
        except Exception as e:
            purge_errors.append(f"{name}:{type(e).__name__}")

    # Replay check: re-read the key row and try to decrypt the previously captured ciphertexts.
    after = await repos.dsr.read_key(key_id)
    wrapped_after = after["wrapped_dek"] if after else None
    results = []
    for blob, aad in captured:
        results.append(await _replay(keys, blob, wrapped_after, aad))
    unreadable = wrapped_after is None and all(r == crypto.ERASED_MARKER for r in results)
    now = datetime.now(UTC)
    audit = AuditRecord(
        action="dsr.erase", decision="ALLOW", tenant_id=ctx.tenant_id, actor_id=ctx.principal.sub, actor_type="USER",
        resource_type="driver", resource_ids=[driver_id], purpose=ctx.purpose.value, reason="crypto-shredding erasure",
        attributes={"dsr_id": str(dsr["id"]), "subject_key_sha256": hashlib.sha256(key_id.encode()).hexdigest()[:16]},
    )
    await ctx.deps.audit.publish(audit)
    evidence = {
        "method": "crypto-shredding",
        "subject_key_destroyed_at": (after or {}).get("destroyed_at").isoformat() if after and after.get("destroyed_at") else now.isoformat(),
        "already_erased": already,
        "key_destroyed_now": bool(destroyed),
        "readable_before_erasure": readable_before,
        "rows_nulled": {"driver_pii": nulled, "trips_pseudonymized": trips, "cache_and_twin_entries_purged": purged},
        "projection_purge_errors": purge_errors,
        "replay_check": {
            "ciphertexts_checked": len(results), "results": sorted(set(results)) if results else [],
            "unreadable": unreadable, "ciphertext_sha256": [_sha(b) for b, _ in captured],
        },
        "audit_event_id": audit.event_id,
    }
    row = await repos.dsr.finish(str(dsr["id"]), "completed" if unreadable else "verified", evidence)
    return dsr_out(row)


async def _replay(keys: crypto.SubjectKeyStore, blob: bytes, wrapped: bytes | None, aad: bytes) -> str:
    try:
        r = await asyncio.to_thread(crypto.decrypt_or_erased, keys, blob, wrapped, aad)
    except crypto.CryptoError:
        return "[unreadable]"
    return r if r == crypto.ERASED_MARKER else "[READABLE]"


async def _access(ctx: Ctx, repos: Any, dsr: dict[str, Any], subject: dict[str, Any]) -> dict[str, Any]:
    """Right of access: export the subject data. The export is returned ONCE; only counts + a hash are persisted."""
    exp = await repos.dsr.subject_export(str(subject["id"]))
    name = await asyncio.to_thread(crypto.decrypt_or_erased, ctx.deps.keys, subject.get("name_enc"), subject.get("wrapped_dek"), crypto.AAD_DRIVER_NAME)
    lic = await asyncio.to_thread(crypto.decrypt_or_erased, ctx.deps.keys, subject.get("license_enc"), subject.get("wrapped_dek"), crypto.AAD_DRIVER_LICENSE)
    export = {"driver": {**exp["driver"], "name": name, "license": lic}, "trips": exp["trips"], "assignments": exp.get("assignments", [])}
    blob = json.dumps(export, sort_keys=True, default=str).encode()
    persisted = {"kind": "ACCESS", "exported_at": datetime.now(UTC).isoformat(), "trips": len(export["trips"]),
                 "assignments": len(export["assignments"]), "export_sha256": hashlib.sha256(blob).hexdigest()}
    row = await repos.dsr.finish(str(dsr["id"]), "completed", persisted)
    await ctx.deps.audit.publish(AuditRecord(
        action="dsr.access", decision="ALLOW", tenant_id=ctx.tenant_id, actor_id=ctx.principal.sub, actor_type="USER",
        resource_type="driver", resource_ids=[str(subject["id"])], purpose=ctx.purpose.value, reason="subject access export",
        attributes={"dsr_id": str(dsr["id"])}))
    return dsr_out(row, {"export": json.loads(blob)})


# ---------------------------------------------------------------- insight export
def _parse_range(date_from: date, date_to: date) -> tuple[str, str]:
    if date_to < date_from:
        raise unprocessable("'to' must not be before 'from'", "invalid-range")
    if (date_to - date_from).days > MAX_EXPORT_DAYS:
        raise unprocessable(f"range exceeds {MAX_EXPORT_DAYS} days", "invalid-range")
    return date_from.isoformat(), date_to.isoformat()


async def insight_export(ctx: Ctx, date_from: date, date_to: date) -> dict[str, Any]:
    f, t = _parse_range(date_from, date_to)
    async with ctx.tx() as repos:
        rows = await repos.reports.insight_rows(f, t)
    ctx.note_resources("insight_export")
    return k_anonymize(rows, ctx.deps.settings.export_k)
