"""Transport-neutral message shape (the Kafka adapter converts from confluent_kafka.Message)."""
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
