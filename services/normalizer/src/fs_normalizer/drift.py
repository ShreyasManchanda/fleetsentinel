"""Schema-drift detection: per-OEM failure-rate window + unknown source-field detection.

A drift incident opens when, inside the trailing window, at least ``min_events`` records were seen and the failure
ratio is >= ``threshold``. The incident closes (and can re-open) once the ratio falls below ``threshold / 2`` with
enough events, or after ``cooldown_s``. The alert id is deterministic on (oem, window start), so a restart re-emitting
it is idempotent downstream.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field


@dataclass(slots=True)
class DriftEvent:
    oem: str
    window_start_ms: int
    total: int
    failed: int
    ratio: float
    reasons: dict[str, int]
    unknown_fields: list[str]
    sw_version: str | None
    mapping_version: int | None


@dataclass(slots=True)
class _OemState:
    buckets: deque = field(default_factory=deque)            # (bucket_start_ms, ok, failed)
    reasons: Counter = field(default_factory=Counter)
    unknown: set = field(default_factory=set)
    sw_version: str | None = None
    active: bool = False
    opened_ms: int = 0


class DriftMonitor:
    def __init__(self, window_s: int = 60, min_events: int = 5, threshold: float = 0.5, cooldown_s: int = 600) -> None:
        self.window_ms = window_s * 1000
        self.min_events = min_events
        self.threshold = threshold
        self.cooldown_ms = cooldown_s * 1000
        self._s: dict[str, _OemState] = {}

    def ratio(self, oem: str, now_ms: int) -> tuple[int, int, float]:
        st = self._s.get(oem)
        if st is None:
            return 0, 0, 0.0
        self._expire(st, now_ms)
        ok = sum(b[1] for b in st.buckets)
        bad = sum(b[2] for b in st.buckets)
        tot = ok + bad
        return tot, bad, (bad / tot if tot else 0.0)

    def _expire(self, st: _OemState, now_ms: int) -> None:
        cutoff = now_ms - self.window_ms
        while st.buckets and st.buckets[0][0] + 1000 <= cutoff:
            st.buckets.popleft()

    def observe(self, oem: str, ok: int, failed: int, now_ms: int, reasons: dict[str, int] | None = None,
                unknown_fields: list[str] | None = None, sw_version: str | None = None,
                mapping_version: int | None = None) -> DriftEvent | None:
        st = self._s.setdefault(oem, _OemState())
        sec = now_ms - now_ms % 1000
        if st.buckets and st.buckets[-1][0] == sec:
            b = st.buckets[-1]
            st.buckets[-1] = (sec, b[1] + ok, b[2] + failed)
        else:
            st.buckets.append((sec, ok, failed))
        if reasons:
            st.reasons.update(reasons)
        if unknown_fields:
            st.unknown.update(unknown_fields)
        if sw_version:
            st.sw_version = sw_version
        total, bad, ratio = self.ratio(oem, now_ms)
        if total == 0:
            return None
        if st.active:
            if (total >= self.min_events and ratio < self.threshold / 2) or now_ms - st.opened_ms >= self.cooldown_ms:
                st.active = False
                st.reasons.clear()
                st.unknown.clear()
            return None
        if total >= self.min_events and ratio >= self.threshold:
            st.active = True
            st.opened_ms = now_ms
            start = st.buckets[0][0] if st.buckets else sec
            return DriftEvent(
                oem=oem, window_start_ms=start - start % self.window_ms, total=total, failed=bad, ratio=ratio,
                reasons=dict(st.reasons.most_common(5)), unknown_fields=sorted(st.unknown)[:20],
                sw_version=st.sw_version, mapping_version=mapping_version,
            )
        return None