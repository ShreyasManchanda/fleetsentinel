"""Bloom filter used ONLY as a negative filter: "not present" is trusted, "maybe present" must be confirmed exactly.

Sizing: for ``n`` items and false-positive rate ``p``: ``m = -n ln p / (ln 2)^2`` bits, ``k = (m/n) ln 2`` hashes
(1% at 9.6 bits/item, k = 7). ``add``/``__contains__`` are O(k). No false negatives, ever.
"""
from __future__ import annotations

import hashlib
import math


def optimal_params(n: int, p: float) -> tuple[int, int]:
    """Return (m_bits, k) for ``n`` expected items at false-positive rate ``p``."""
    if n < 1 or not (0.0 < p < 1.0):
        raise ValueError("need n >= 1 and 0 < p < 1")
    m = math.ceil(-n * math.log(p) / (math.log(2) ** 2))
    k = max(1, round((m / n) * math.log(2)))
    return m, k


class BloomFilter:
    def __init__(self, m_bits: int, k: int) -> None:
        if m_bits < 8 or k < 1:
            raise ValueError("m_bits >= 8 and k >= 1 required")
        self.m, self.k = m_bits, k
        self._bits = bytearray((m_bits + 7) // 8)
        self.count = 0

    @classmethod
    def for_capacity(cls, n: int, p: float = 0.01) -> BloomFilter:
        return cls(*optimal_params(n, p))

    def _indexes(self, item: bytes | str):
        data = item.encode() if isinstance(item, str) else item
        d = hashlib.blake2b(data, digest_size=16).digest()
        h1 = int.from_bytes(d[:8], "little")
        h2 = int.from_bytes(d[8:], "little") | 1  # odd => full period for double hashing
        m = self.m
        for i in range(self.k):
            yield (h1 + i * h2) % m

    def add(self, item: bytes | str) -> None:
        for ix in self._indexes(item):
            self._bits[ix >> 3] |= 1 << (ix & 7)
        self.count += 1

    def __contains__(self, item: bytes | str) -> bool:
        bits = self._bits
        return all(bits[ix >> 3] & (1 << (ix & 7)) for ix in self._indexes(item))

    def expected_fp_rate(self) -> float:
        """Theoretical FP rate at the current fill: (1 - e^(-kn/m))^k."""
        return (1.0 - math.exp(-self.k * self.count / self.m)) ** self.k

    def clear(self) -> None:
        self._bits = bytearray(len(self._bits))
        self.count = 0


class RotatingBloom:
    """Two generations so memory stays bounded for an unbounded stream: current + previous, rotate at capacity.

    Still a pure negative filter: an item evicted by rotation looks "new" and the caller exact TTL store decides.
    """

    def __init__(self, capacity: int, p: float = 0.01) -> None:
        self._cap = capacity
        self._cur = BloomFilter.for_capacity(capacity, p)
        self._prev = BloomFilter.for_capacity(capacity, p)

    def add(self, item: bytes | str) -> None:
        if self._cur.count >= self._cap:
            self._prev, self._cur = self._cur, self._prev
            self._cur.clear()
        self._cur.add(item)

    def __contains__(self, item: bytes | str) -> bool:
        return item in self._cur or item in self._prev
