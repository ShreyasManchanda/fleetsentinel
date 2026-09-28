"""Circuit-breaker wiring around a TenantDb, plus breaker factory helpers."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from .config import Settings
from .domain.breaker import CircuitBreaker
from .infra_errors import is_infra_error
from .metrics import BREAKER_STATES, Metrics
from .ports import Repos, TenantDb


def make_breaker(name: str, s: Settings, metrics: Metrics | None = None) -> CircuitBreaker:
    def changed(n: str, state: str) -> None:
        if metrics is not None:
            metrics.breaker.labels(n).set(BREAKER_STATES[state])

    if metrics is not None:
        metrics.breaker.labels(name).set(0)
    return CircuitBreaker(name, s.breaker_failures, s.breaker_reset_s, should_trip=is_infra_error, on_state_change=changed)


class GuardedTenantDb:
    """Fast-fail 503 (via CircuitOpen) instead of piling requests onto a dead Postgres; business errors never trip the breaker."""

    def __init__(self, inner: TenantDb, breaker: CircuitBreaker):
        self.inner, self.breaker = inner, breaker

    @asynccontextmanager
    async def tx(self, tenant_id: str) -> AsyncIterator[Repos]:
        self.breaker.before_call()
        try:
            async with self.inner.tx(tenant_id) as repos:
                yield repos
        except BaseException as e:
            self.breaker.record_failure(e)
            raise
        else:
            self.breaker.record_success()

    async def ping(self) -> None:
        await self.breaker.call(self.inner.ping)
