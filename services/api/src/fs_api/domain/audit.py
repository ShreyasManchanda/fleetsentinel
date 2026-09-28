"""Audit events (audit.events.v1 Protobuf). Emitted for EVERY request; the sink builds the hash chain."""
from __future__ import annotations

import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

from fleetsentinel.v1 import audit_pb2 as pb

ACTOR = {"USER": pb.USER, "SERVICE": pb.SERVICE, "AGENT": pb.AGENT, "SYSTEM": pb.SYSTEM}
ACTOR_NAME = {v: k for k, v in ACTOR.items()}


def uuid7() -> str:
    """Time-ordered unique id (RFC 9562 v7 layout)."""
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms << 80) | (0x7 << 76) | (((rand >> 62) & 0xFFF) << 64) | (0b10 << 62) | (rand & ((1 << 62) - 1))
    return str(uuid.UUID(int=value))


@dataclass(slots=True)
class AuditRecord:
    action: str
    decision: str                      # ALLOW | DENY
    tenant_id: str = ""
    actor_id: str = "anonymous"
    actor_type: str = "USER"
    resource_type: str = ""
    resource_ids: list[str] = field(default_factory=list)
    purpose: str = ""
    reason: str = ""
    latency_ms: int = 0
    attributes: dict[str, str] = field(default_factory=dict)
    event_id: str = field(default_factory=uuid7)
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))


def to_proto(r: AuditRecord) -> pb.AuditEvent:
    return pb.AuditEvent(
        event_id=r.event_id, ts_ms=r.ts_ms, tenant_id=r.tenant_id, actor_id=r.actor_id,
        actor_type=ACTOR.get(r.actor_type, pb.ACTOR_TYPE_UNSPECIFIED), action=r.action, resource_type=r.resource_type,
        resource_ids=r.resource_ids, purpose=r.purpose, decision=pb.ALLOW if r.decision == "ALLOW" else pb.DENY,
        reason=r.reason, latency_ms=max(0, int(r.latency_ms)), attributes={k: str(v) for k, v in r.attributes.items()},
    )


def from_proto(data: bytes) -> AuditRecord:
    e = pb.AuditEvent()
    e.ParseFromString(data)
    return AuditRecord(
        action=e.action, decision="ALLOW" if e.decision == pb.ALLOW else "DENY", tenant_id=e.tenant_id,
        actor_id=e.actor_id, actor_type=ACTOR_NAME.get(e.actor_type, "USER"), resource_type=e.resource_type,
        resource_ids=list(e.resource_ids), purpose=e.purpose, reason=e.reason, latency_ms=e.latency_ms,
        attributes=dict(e.attributes), event_id=e.event_id, ts_ms=e.ts_ms,
    )


class AuditPublisher(Protocol):
    async def publish(self, rec: AuditRecord) -> None: ...


class ResilientAuditPublisher:
    """Never blocks the request path and never silently drops: on a publish failure records queue in a bounded buffer that
    is drained (oldest first) on the next successful publish. Overflow is counted (`dropped`) and must alert."""

    def __init__(self, primary: AuditPublisher, max_buffer: int = 10_000):
        self._primary = primary
        self._buf: deque[AuditRecord] = deque()
        self._max = max_buffer
        self.dropped = 0
        self.published = 0

    @property
    def buffered(self) -> int:
        return len(self._buf)

    async def publish(self, rec: AuditRecord) -> None:
        self._buf.append(rec)
        while len(self._buf) > self._max:
            self._buf.popleft()
            self.dropped += 1
        await self.flush()

    async def flush(self) -> None:
        while self._buf:
            head = self._buf[0]
            try:
                await self._primary.publish(head)
            except Exception:
                return
            self._buf.popleft()
            self.published += 1


class ListAuditPublisher:
    """In-memory publisher (tests and dev without Kafka)."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def publish(self, rec: AuditRecord) -> None:
        self.records.append(rec)


def resource_type_of(route_template: str) -> str:
    parts = [p for p in route_template.split("/") if p and not p.startswith("{") and p != "v1"]
    return parts[0].rstrip("s") if parts else ""


def attributes_of(path_params: dict[str, Any]) -> dict[str, str]:
    return {k: str(v) for k, v in path_params.items()}
