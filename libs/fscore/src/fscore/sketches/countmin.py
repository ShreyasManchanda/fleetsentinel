"""Count-Min Sketch and a top-K tracker (min-heap) on top of it.

CMS with width ``w`` and depth ``d``: overcount error <= (e/w) * N with probability >= 1 - e^-d; it NEVER undercounts.
``add`` is O(d); ``estimate`` is O(d); the top-K structure adds O(log K) amortised per update.
Defaults w=2048, d=5 => <=0.13% of stream length overcount with 99.3% confidence.
"""
from __future__ import annotations

import hashlib
import heapq

import numpy as np


class CountMinSketch:
    def __init__(self, width: int = 2048, depth: int = 5) -> None:
        if width < 1 or not (1 <= depth <= 8):
            raise ValueError("width >= 1 and 1 <= depth <= 8 required")
        self.w, self.d = width, depth
        self.table = np.zeros((depth, width), dtype=np.uint64)
        self.total = 0

    def _cols(self, item: str | bytes) -> list[int]:
        data = item.encode() if isinstance(item, str) else item
        digest = hashlib.blake2b(data, digest_size=4 * self.d).digest()
        w = self.w
        return [int.from_bytes(digest[4 * i : 4 * i + 4], "little") % w for i in range(self.d)]

    def add(self, item: str | bytes, count: int = 1) -> int:
        """Add ``count`` occurrences; returns the new estimate."""
        if count < 0:
            raise ValueError("count must be >= 0")
        est: int | None = None
        for row, col in enumerate(self._cols(item)):
            self.table[row, col] += count
            v = int(self.table[row, col])
            est = v if est is None else min(est, v)
        self.total += count
        return est or 0

    def estimate(self, item: str | bytes) -> int:
        return min(int(self.table[row, col]) for row, col in enumerate(self._cols(item)))

    def merge(self, other: CountMinSketch) -> None:
        if (self.w, self.d) != (other.w, other.d):
            raise ValueError("shape mismatch")
        self.table += other.table
        self.total += other.total

    def to_bytes(self) -> bytes:
        return self.table.tobytes()

    @classmethod
    def from_bytes(cls, raw: bytes, width: int, depth: int, total: int = 0) -> CountMinSketch:
        s = cls(width, depth)
        s.table = np.frombuffer(raw, dtype=np.uint64).reshape(depth, width).copy()
        s.total = total
        return s


class TopK:
    """Heavy hitters: ``k`` items with the largest CMS estimates, kept in a min-heap (lazy deletion)."""

    def __init__(self, k: int, cms: CountMinSketch | None = None) -> None:
        if k < 1:
            raise ValueError("k >= 1")
        self.k = k
        self.cms = cms or CountMinSketch()
        self._est: dict[str, int] = {}
        self._heap: list[tuple[int, str]] = []

    def update(self, item: str, count: int = 1) -> None:
        est = self.cms.add(item, count)
        if item in self._est:
            self._est[item] = est
            heapq.heappush(self._heap, (est, item))
        elif len(self._est) < self.k:
            self._est[item] = est
            heapq.heappush(self._heap, (est, item))
        else:
            min_est, min_item = self._peek_min()
            if est > min_est:
                del self._est[min_item]
                self._est[item] = est
                heapq.heappush(self._heap, (est, item))
        if len(self._heap) > 8 * self.k + 64:
            self._compact()

    def _peek_min(self) -> tuple[int, str]:
        while self._heap:
            est, item = self._heap[0]
            if self._est.get(item) == est:
                return est, item
            heapq.heappop(self._heap)  # stale entry
        raise RuntimeError("heap empty")  # pragma: no cover

    def _compact(self) -> None:
        self._heap = [(e, i) for i, e in self._est.items()]
        heapq.heapify(self._heap)

    def top(self, n: int | None = None) -> list[tuple[str, int]]:
        """Items by descending estimate (ties broken by item for determinism)."""
        items = sorted(self._est.items(), key=lambda kv: (-kv[1], kv[0]))
        return items[: n or self.k]

    def snapshot(self) -> dict[str, int]:
        return dict(self._est)

    def restore_items(self, est: dict[str, int]) -> None:
        self._est = dict(est)
        self._compact()
