"""Exact anti-replay dedup (IPsec/DTLS style sliding window): max sequence seen + 64-bit bitmap.

Bit ``i`` of the bitmap means "sequence ``max_seq - i`` has been seen". Everything inside the window is decided
exactly (no false positives, no false negatives); a sequence older than the window is reported as ``TOO_OLD`` so the
caller can count it as a late replay instead of silently guessing.

Complexity: ``check_and_update`` is O(1) time; state is two integers per VIN (~16 B, ~1.6 MB per 100k vehicles).
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from enum import Enum

WINDOW = 64
_MASK = (1 << WINDOW) - 1


class Verdict(Enum):
    NEW = "new"              # never seen, accepted
    DUPLICATE = "duplicate"  # seen before, drop
    TOO_OLD = "too_old"      # older than the window: cannot be decided exactly, caller policy applies


class AntiReplayWindow:
    """Sliding replay window for ONE stream (one VIN)."""

    __slots__ = ("max_seq", "bitmap")

    def __init__(self, max_seq: int = -1, bitmap: int = 0) -> None:
        self.max_seq = max_seq
        self.bitmap = bitmap & _MASK

    def check_and_update(self, seq: int) -> Verdict:
        """Record ``seq`` and classify it. O(1)."""
        if seq > self.max_seq:
            shift = seq - self.max_seq
            if self.max_seq >= 0 and shift < WINDOW:
                self.bitmap = ((self.bitmap << shift) | 1) & _MASK
            else:
                self.bitmap = 1
            self.max_seq = seq
            return Verdict.NEW
        diff = self.max_seq - seq
        if diff >= WINDOW:
            return Verdict.TOO_OLD
        bit = 1 << diff
        if self.bitmap & bit:
            return Verdict.DUPLICATE
        self.bitmap |= bit
        return Verdict.NEW

    def seen(self, seq: int) -> bool | None:
        """Read-only membership: True/False inside the window, None if too old to know."""
        if seq > self.max_seq:
            return False
        diff = self.max_seq - seq
        if diff >= WINDOW:
            return None
        return bool(self.bitmap & (1 << diff))

    def to_tuple(self) -> tuple[int, int]:
        return self.max_seq, self.bitmap


class AntiReplayStore:
    """A window per key (VIN). ``snapshot``/``restore`` make it checkpointable."""

    def __init__(self) -> None:
        self._w: dict[str, AntiReplayWindow] = {}

    def __len__(self) -> int:
        return len(self._w)

    def check_and_update(self, key: str, seq: int) -> Verdict:
        w = self._w.get(key)
        if w is None:
            w = self._w[key] = AntiReplayWindow()
        return w.check_and_update(seq)

    def snapshot(self) -> dict[str, tuple[int, int]]:
        return {k: w.to_tuple() for k, w in self._w.items()}

    @classmethod
    def restore(cls, data: dict[str, Iterable[int]]) -> AntiReplayStore:
        s = cls()
        for k, v in data.items():
            m, b = tuple(v)
            s._w[k] = AntiReplayWindow(m, b)
        return s


class RecentSet:
    """Exact bounded set for streams WITHOUT sequence numbers: FIFO-evicting set of hashable keys.

    O(1) add/check. Keys evicted after ``capacity`` newer insertions can no longer be recognised (documented bound).
    """

    def __init__(self, capacity: int = 100_000) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._cap = capacity
        self._d: OrderedDict[object, None] = OrderedDict()

    def __len__(self) -> int:
        return len(self._d)

    def add_if_new(self, key: object) -> bool:
        """True if ``key`` was not present (and is now recorded); False if it is a duplicate."""
        if key in self._d:
            return False
        self._d[key] = None
        if len(self._d) > self._cap:
            self._d.popitem(last=False)
        return True

    def snapshot(self) -> list[object]:
        return list(self._d)

    @classmethod
    def restore(cls, keys: Iterable[object], capacity: int = 100_000) -> RecentSet:
        s = cls(capacity)
        for k in keys:
            s._d[tuple(k) if isinstance(k, list) else k] = None
        return s
