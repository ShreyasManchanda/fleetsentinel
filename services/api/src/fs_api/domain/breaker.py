"""Circuit breaker (closed -> open -> half-open) for Postgres / Mongo / Redis / agent calls."""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")
CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class CircuitOpen(Exception):
    def __init__(self, name: str, retry_after_s: float):
        super().__init__(f"circuit '{name}' is open")
        self.name = name
        self.retry_after_s = retry_after_s


class CircuitBreaker:
    """Opens after `failure_threshold` consecutive infrastructure failures; after `reset_timeout_s` lets ONE probe
    call through (half-open). A successful probe closes it, a failed probe re-opens it. Callers get a fast
    :class:`CircuitOpen` instead of waiting on a dead dependency."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        reset_timeout_s: float = 10.0,
        should_trip: Callable[[BaseException], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_state_change: Callable[[str, str], None] | None = None,
    ):
        self.name = name
        self.threshold = failure_threshold
        self.reset_timeout = reset_timeout_s
        self._should_trip = should_trip or (lambda e: True)
        self._clock = clock
        self._on_change = on_state_change
        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probing = False

    @property
    def state(self) -> str:
        if self._state == OPEN and self._clock() - self._opened_at >= self.reset_timeout:
            return HALF_OPEN
        return self._state

    def _set(self, new: str) -> None:
        if new != self._state:
            self._state = new
            if self._on_change:
                self._on_change(self.name, new)

    def before_call(self) -> None:
        st = self.state
        if st == CLOSED:
            return
        if st == HALF_OPEN and not self._probing:
            self._set(HALF_OPEN)
            self._probing = True
            return
        raise CircuitOpen(self.name, max(0.0, self.reset_timeout - (self._clock() - self._opened_at)))

    def record_success(self) -> None:
        self._failures = 0
        self._probing = False
        self._set(CLOSED)

    def record_failure(self, exc: BaseException | None = None) -> None:
        if exc is not None and not self._should_trip(exc):
            # a business error is not an infrastructure failure; a half-open probe that got one proves the dependency is alive
            if self._probing:
                self.record_success()
            return
        was_probing = self._probing
        self._probing = False
        self._failures += 1
        if was_probing or self._failures >= self.threshold:
            self._opened_at = self._clock()
            self._set(OPEN)

    async def call(self, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        self.before_call()
        try:
            result = await fn(*args, **kwargs)
        except BaseException as e:
            self.record_failure(e)
            raise
        self.record_success()
        return result


class BreakerProxy:
    """Wrap every coroutine method of an adapter so its calls go through a breaker (transparent to callers)."""

    def __init__(self, inner: Any, breaker: CircuitBreaker):
        self._inner = inner
        self.breaker = breaker

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr
        import inspect

        if inspect.iscoroutinefunction(attr):
            async def guarded(*a: Any, **kw: Any):
                return await self.breaker.call(attr, *a, **kw)

            return guarded
        return attr
