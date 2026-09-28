"""Hierarchical timing wheel driven by an EXTERNAL clock (the event-time watermark), never by wall time.

Layout: ``levels`` wheels of ``2^slot_bits`` slots each; level ``i`` slot width is ``2^(slot_bits*i)`` ticks.
An entry lives in exactly one slot (a dict, so cancel is O(1)); on a wheel wrap the next-coarser slot is cascaded
down. Deadlines beyond the total horizon wait in an overflow dict that is rescanned once per full top-level turn.

Complexity: ``schedule`` / ``cancel`` O(1); ``advance`` O(ticks elapsed + entries fired + entries cascaded), each
entry cascading at most ``levels - 1`` times. Memory O(entries).

Guarantees (property-tested): an entry fires exactly once, never before ``deadline_ms <= watermark``, and at most one
tick after the watermark first reaches its deadline.
"""
from __future__ import annotations

from collections.abc import Hashable

_REBUILD_THRESHOLD_TICKS = 4096


class TimingWheel:
    def __init__(self, tick_ms: int = 1000, slot_bits: int = 6, levels: int = 4) -> None:
        if tick_ms < 1 or slot_bits < 1 or levels < 1:
            raise ValueError("tick_ms, slot_bits and levels must be >= 1")
        self.tick_ms = tick_ms
        self._b = slot_bits
        self._levels = levels
        self._size = 1 << slot_bits
        self._mask = self._size - 1
        self._horizon = 1 << (slot_bits * levels)
        self._wheels: list[list[dict[Hashable, int]]] = [
            [{} for _ in range(self._size)] for _ in range(levels)
        ]
        self._overflow: dict[Hashable, int] = {}
        self._loc: dict[Hashable, tuple[int | str, int] | None] = {}   # key -> (level, slot); None = overflow
        self._deadline: dict[Hashable, int] = {}
        self._due: dict[Hashable, None] = {}      # already overdue at schedule time: fire on the next advance
        self._pending: dict[Hashable, int] = {}   # scheduled before the clock started
        self._cur = 0
        self._started = False

    # ------------------------------------------------------------------ public
    def __len__(self) -> int:
        return len(self._deadline)

    def __contains__(self, key: Hashable) -> bool:
        return key in self._deadline

    def deadline(self, key: Hashable) -> int | None:
        return self._deadline.get(key)

    def schedule(self, key: Hashable, deadline_ms: int) -> None:
        """(Re)schedule ``key`` to fire once the watermark reaches ``deadline_ms``. O(1)."""
        if key in self._deadline:
            self._remove(key)
        e = -(-deadline_ms // self.tick_ms)  # ceil: never fire early
        self._deadline[key] = deadline_ms
        if not self._started:
            self._pending[key] = e
            self._loc[key] = ("pending", 0)
        elif e <= self._cur:
            self._due[key] = None            # e <= cur implies deadline <= watermark already
            self._loc[key] = ("due", 0)
        else:
            self._insert(key, e)

    def cancel(self, key: Hashable) -> bool:
        if key not in self._deadline:
            return False
        self._remove(key)
        del self._deadline[key]
        return True

    def advance(self, watermark_ms: int) -> list[tuple[Hashable, int]]:
        """Move time forward to ``watermark_ms``; return ``[(key, deadline_ms)]`` that expired (ordered by tick, key)."""
        target = watermark_ms // self.tick_ms
        fired: list[tuple[Hashable, int]] = []
        if not self._started:
            self._cur, self._started = target, True
            pend, self._pending = self._pending, {}
            for key, e in sorted(pend.items(), key=lambda kv: (kv[1], repr(kv[0]))):
                del self._loc[key]
                if e <= target:
                    fired.append((key, self._deadline.pop(key)))
                else:
                    self._insert(key, e)
            return fired
        if self._due:
            for key in sorted(self._due, key=lambda k: (self._deadline[k], repr(k))):
                del self._loc[key]
                fired.append((key, self._deadline.pop(key)))
            self._due = {}
        if target <= self._cur:
            return fired
        if not self._deadline:
            self._cur = target
            return fired
        if target - self._cur > _REBUILD_THRESHOLD_TICKS:
            return fired + self._rebuild(target)
        while self._cur < target:
            self._cur += 1
            self._tick(fired)
        return fired

    # ------------------------------------------------------------------ internals
    def _insert(self, key: Hashable, e: int) -> None:
        delta = e - self._cur
        for lvl in range(self._levels):
            if delta < (1 << (self._b * (lvl + 1))):
                slot = (e >> (self._b * lvl)) & self._mask
                self._wheels[lvl][slot][key] = e
                self._loc[key] = (lvl, slot)
                return
        self._overflow[key] = e
        self._loc[key] = None

    def _remove(self, key: Hashable) -> None:
        loc = self._loc.pop(key)
        if loc is None:
            self._overflow.pop(key, None)
        elif loc[0] == "pending":
            self._pending.pop(key, None)
        elif loc[0] == "due":
            self._due.pop(key, None)
        else:
            self._wheels[loc[0]][loc[1]].pop(key, None)  # type: ignore[index]

    def _tick(self, fired: list[tuple[Hashable, int]]) -> None:
        cur = self._cur
        if cur & (self._horizon - 1) == 0 and self._overflow:
            for key, e in list(self._overflow.items()):
                del self._overflow[key]
                del self._loc[key]
                self._insert(key, e)
        for lvl in range(self._levels - 1, 0, -1):
            if cur & ((1 << (self._b * lvl)) - 1) == 0:
                slot = (cur >> (self._b * lvl)) & self._mask
                bucket = self._wheels[lvl][slot]
                if bucket:
                    self._wheels[lvl][slot] = {}
                    for key, e in bucket.items():
                        del self._loc[key]
                        self._insert(key, e)
        slot0 = self._wheels[0][cur & self._mask]
        if slot0:
            self._wheels[0][cur & self._mask] = {}
            for key in sorted(slot0, key=repr):
                del self._loc[key]
                fired.append((key, self._deadline.pop(key)))

    def _rebuild(self, target: int) -> list[tuple[Hashable, int]]:
        entries: list[tuple[int, Hashable]] = []
        for wheel in self._wheels:
            for i, bucket in enumerate(wheel):
                if bucket:
                    entries.extend((e, k) for k, e in bucket.items())
                    wheel[i] = {}
        entries.extend((e, k) for k, e in self._overflow.items())
        self._overflow.clear()
        self._loc.clear()
        self._cur = target
        fired: list[tuple[Hashable, int]] = []
        for e, key in sorted(entries, key=lambda t: (t[0], repr(t[1]))):
            if e <= target:
                fired.append((key, self._deadline.pop(key)))
            else:
                self._insert(key, e)
        return fired
