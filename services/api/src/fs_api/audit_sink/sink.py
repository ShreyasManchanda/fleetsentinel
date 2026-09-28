"""audit.events.v1 -> audit_log sink. The hash chain is built by the DB trigger at INSERT time (prev_hash || canonical row under an
advisory lock), so the sink only has to append events IN ORDER, at-least-once, and skip redeliveries (idempotent on event id)."""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from ..domain.audit import AuditRecord, from_proto

log = logging.getLogger("fs_api.audit_sink")


class AuditWriter(Protocol):
    def write_batch(self, rows: list[dict[str, Any]]) -> int:
        """Append rows in order inside ONE transaction; rows whose event id already exists are skipped. Returns rows inserted."""
        ...


@dataclass(slots=True)
class BatchOutcome:
    received: int = 0
    inserted: int = 0
    poison: int = 0
    errors: list[str] = field(default_factory=list)


def _uuid_or_none(v: str) -> str | None:
    try:
        return str(uuid.UUID(v))
    except (ValueError, AttributeError, TypeError):
        return None


def record_to_row(rec: AuditRecord) -> dict[str, Any]:
    """Map an audit event to the audit_log columns (seq, prev_hash and hash are DB-generated)."""
    attrs = dict(rec.attributes)
    tenant = _uuid_or_none(rec.tenant_id) if rec.tenant_id else None
    if rec.tenant_id and tenant is None:
        attrs["raw_tenant_id"] = rec.tenant_id  # never lose information, never write an invalid uuid
    return {
        "id": _uuid_or_none(rec.event_id) or str(uuid.uuid4()),
        "ts": datetime.fromtimestamp(rec.ts_ms / 1000.0, tz=UTC),
        "tenant_id": tenant,
        "actor_id": rec.actor_id or None,
        "actor_type": rec.actor_type or None,
        "action": rec.action or "unknown",
        "resource_type": rec.resource_type or None,
        "resource_ids": list(rec.resource_ids),
        "purpose": rec.purpose or None,
        "decision": rec.decision,
        "reason": rec.reason or None,
        "latency_ms": rec.latency_ms,
        "attributes": attrs,
    }


class AuditSink:
    def __init__(self, writer: AuditWriter, on_poison: Callable[[bytes, str], None] | None = None):
        self._w = writer
        self._on_poison = on_poison

    def handle(self, payloads: list[bytes]) -> BatchOutcome:
        """Decode and append a batch. Undecodable messages are counted and reported (poison), never block the partition."""
        out = BatchOutcome(received=len(payloads))
        rows: list[dict[str, Any]] = []
        for p in payloads:
            try:
                rows.append(record_to_row(from_proto(p)))
            except Exception as e:  # protobuf DecodeError etc.
                out.poison += 1
                out.errors.append(type(e).__name__)
                if self._on_poison:
                    self._on_poison(p, repr(e))
                log.warning("poison audit event skipped: %s", type(e).__name__)
        if rows:
            out.inserted = self._w.write_batch(rows)
        return out
