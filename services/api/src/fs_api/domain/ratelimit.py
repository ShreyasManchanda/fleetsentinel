"""Token-bucket rate limiting. Redis holds the buckets in production (atomic Lua); the in-memory store implements the
same algorithm for tests and as the degraded fallback when Redis is unavailable (fail-open per instance, never fail-closed)."""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_s: int  # 0 when allowed
    reset_s: int        # seconds until the bucket is full again


@dataclass(frozen=True, slots=True)
class Limit:
    scope: str          # tenant | user | copilot
    key: str
    per_minute: int

    @property
    def refill_per_s(self) -> float:
        return self.per_minute / 60.0


class BucketStore(Protocol):
    async def take(self, key: str, capacity: int, refill_per_s: float, cost: int = 1) -> Decision: ...


def bucket_step(tokens: float, last_ts: float, now: float, capacity: int, refill_per_s: float, cost: int
                ) -> tuple[bool, float, float]:
    """Pure token-bucket step. Returns (allowed, new_tokens, retry_after_s). Mirrored 1:1 by the Redis Lua script."""
    tokens = min(float(capacity), tokens + max(0.0, now - last_ts) * refill_per_s)
    if tokens >= cost:
        return True, tokens - cost, 0.0
    if refill_per_s <= 0:
        return False, tokens, float("inf")  # zero refill: denied forever, never divide by zero
    return False, tokens, (cost - tokens) / refill_per_s


class InMemoryBucketStore:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._b: dict[str, tuple[float, float]] = {}

    async def take(self, key: str, capacity: int, refill_per_s: float, cost: int = 1) -> Decision:
        now = self._clock()
        tokens, last = self._b.get(key, (float(capacity), now))
        ok, tokens, retry = bucket_step(tokens, last, now, capacity, refill_per_s, cost)
        self._b[key] = (tokens, now)
        reset = math.ceil((capacity - tokens) / refill_per_s) if refill_per_s > 0 else 0
        return Decision(ok, capacity, int(tokens), math.ceil(retry) if not ok else 0, reset)


class RateLimiter:
    """Applies several buckets per request; the most constrained decision drives the RateLimit-* headers."""

    def __init__(self, store: BucketStore, fallback: BucketStore | None = None):
        self._store = store
        self._fallback = fallback or InMemoryBucketStore()
        self.degraded = 0

    async def check(self, limits: list[Limit]) -> tuple[bool, Decision]:
        decisions: list[Decision] = []
        for lim in limits:
            try:
                d = await self._store.take(lim.key, lim.per_minute, lim.refill_per_s)
            except Exception:  # Redis down / breaker open: degrade to per-instance limiting, do not take the API down
                self.degraded += 1
                d = await self._fallback.take(lim.key, lim.per_minute, lim.refill_per_s)
            decisions.append(d)
            if not d.allowed:
                return False, d
        return True, min(decisions, key=lambda d: d.remaining)


def rate_headers(d: Decision) -> dict[str, str]:
    h = {
        "RateLimit-Limit": str(d.limit),
        "RateLimit-Remaining": str(max(0, d.remaining)),
        "RateLimit-Reset": str(d.reset_s),
    }
    if not d.allowed:
        h["Retry-After"] = str(max(1, min(d.retry_after_s, 86_400)))  # clamp inf (zero-refill deny) to a day
    return h
