"""Envelope checks: topic shape, known OEM, shard range, size. The gateway never parses the OEM payload."""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from fscore.contracts import N_SHARDS

MAX_PAYLOAD_BYTES = 1_048_576


class EnvelopeError(ValueError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail = reason, detail


@dataclass(frozen=True, slots=True)
class Envelope:
    kind: str        # telemetry | event
    oem: str
    shard: int
    size: int

    @property
    def kafka_key(self) -> bytes:
        return f"{self.oem}:{self.shard}".encode()


def parse_topic(topic: str) -> tuple[str, str, int]:
    """``telemetry/{oem}/{shard}`` or ``event/{oem}/{shard}`` -> (kind, oem, shard)."""
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] not in ("telemetry", "event"):
        raise EnvelopeError("BAD_TOPIC", topic[:80])
    try:
        shard = int(parts[2])
    except ValueError as e:
        raise EnvelopeError("BAD_SHARD", topic[:80]) from e
    if not (0 <= shard < N_SHARDS):
        raise EnvelopeError("BAD_SHARD", f"shard {shard} outside 0..{N_SHARDS - 1}")
    return parts[0], parts[1], shard


def validate(topic: str, payload: bytes, known_oems: Collection[str], max_bytes: int = MAX_PAYLOAD_BYTES) -> Envelope:
    kind, oem, shard = parse_topic(topic)
    if oem not in known_oems:
        raise EnvelopeError("UNKNOWN_OEM", oem[:40])
    if not payload:
        raise EnvelopeError("EMPTY", "zero-length payload")
    if len(payload) > max_bytes:
        raise EnvelopeError("TOO_LARGE", f"{len(payload)} > {max_bytes}")
    return Envelope(kind, oem, shard, len(payload))