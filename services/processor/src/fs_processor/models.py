"""Transport-neutral message shapes (Kafka adapters convert to/from confluent_kafka.Message)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class InMsg:
    topic: str
    partition: int
    offset: int
    key: bytes | None
    value: bytes | None
    headers: dict[str, bytes] = field(default_factory=dict)
    timestamp_ms: int = 0


@dataclass(slots=True)
class OutMsg:
    topic: str
    key: bytes
    value: bytes
    headers: dict[str, bytes] = field(default_factory=dict)
