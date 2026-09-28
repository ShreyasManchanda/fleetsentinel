"""Event-time tumbling windows with a watermark and allowed lateness.

Watermark = max event time seen - allowed_lateness. A window [start, start+size) is CLOSED once watermark >= its end.
An event whose window is already closed is LATE: it is counted and NOT applied (the batch recompute over the lake is
authoritative for it). Otherwise it is applied exactly once. ``add`` O(1); ``drain_closed`` O(closed windows).
"""
from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Any


class EventTimeWindows:
    def __init__(self, size_ms: int = 60_000, allowed_lateness_ms: int = 10_000,
                 factory: Callable[[], Any] = dict) -> None:
        if size_ms < 1 or allowed_lateness_ms < 0:
            raise ValueError("size_ms >= 1 and allowed_lateness_ms >= 0")
        self.size_ms = size_ms
        self.lateness_ms = allowed_lateness_ms
        self._factory = factory
        self._open: dict[tuple[Hashable, int], Any] = {}
        self.max_ts = -1
        self.late_events = 0
        self.applied_events = 0

    @property
    def watermark(self) -> int:
        return -1 if self.max_ts < 0 else self.max_ts - self.lateness_ms

    def window_start(self, ts_ms: int) -> int:
        return ts_ms - ts_ms % self.size_ms

    def add(self, key: Hashable, ts_ms: int, apply: Callable[[Any], None]) -> bool:
        """Advance the watermark with ``ts_ms`` and apply the event unless its window is closed. True if applied."""
        if ts_ms > self.max_ts:
            self.max_ts = ts_ms
        start = self.window_start(ts_ms)
        if start + self.size_ms <= self.watermark:
            self.late_events += 1
            return False
        k = (key, start)
        agg = self._open.get(k)
        if agg is None:
            agg = self._open[k] = self._factory()
        apply(agg)
        self.applied_events += 1
        return True

    def drain_closed(self) -> list[tuple[Hashable, int, Any]]:
        """Pop windows whose end <= watermark, ordered by (start, key repr) for determinism."""
        wm = self.watermark
        done = [k for k in self._open if k[1] + self.size_ms <= wm]
        done.sort(key=lambda k: (k[1], repr(k[0])))
        return [(k[0], k[1], self._open.pop(k)) for k in done]

    def open_count(self) -> int:
        return len(self._open)

    def snapshot(self) -> dict:
        return {"max_ts": self.max_ts, "late": self.late_events, "applied": self.applied_events,
                "open": [[k[0], k[1], v] for k, v in self._open.items()]}

    def restore(self, data: dict) -> None:
        self.max_ts, self.late_events, self.applied_events = data["max_ts"], data["late"], data["applied"]
        self._open = {(k, s): v for k, s, v in data["open"]}
