"""HyperLogLog with Redis PFADD/PFCOUNT/PFMERGE semantics (production uses Redis; this pure version is for tests
and in-process use). 2^p registers (p=14 => 16384 registers, ~0.81% standard error, ~12 KB in Redis dense form).

``add`` O(1); ``count`` O(m); ``merge`` O(m).
"""
from __future__ import annotations

import hashlib
import math


class HyperLogLog:
    def __init__(self, p: int = 14) -> None:
        if not (4 <= p <= 18):
            raise ValueError("4 <= p <= 18")
        self.p = p
        self.m = 1 << p
        self.registers = bytearray(self.m)

    @staticmethod
    def _hash64(item: str | bytes) -> int:
        data = item.encode() if isinstance(item, str) else item
        return int.from_bytes(hashlib.blake2b(data, digest_size=8).digest(), "little")

    def add(self, item: str | bytes) -> bool:
        """PFADD: True if a register changed."""
        h = self._hash64(item)
        idx = h & (self.m - 1)
        rest = h >> self.p
        bits = 64 - self.p
        rank = (bits - rest.bit_length()) + 1  # position of the leftmost 1-bit in the remaining bits
        if rank > self.registers[idx]:
            self.registers[idx] = rank
            return True
        return False

    def count(self) -> int:
        """PFCOUNT: cardinality estimate with the standard small-range (linear counting) correction."""
        m = self.m
        alpha = 0.7213 / (1 + 1.079 / m)
        inv = sum(2.0 ** -r for r in self.registers)
        est = alpha * m * m / inv
        zeros = self.registers.count(0)
        if est <= 2.5 * m and zeros:
            est = m * math.log(m / zeros)
        return int(round(est))

    def merge(self, other: HyperLogLog) -> None:
        """PFMERGE (union): register-wise max."""
        if other.p != self.p:
            raise ValueError("precision mismatch")
        r, o = self.registers, other.registers
        for i in range(self.m):
            if o[i] > r[i]:
                r[i] = o[i]

    def to_bytes(self) -> bytes:
        return bytes(self.registers)

    @classmethod
    def from_bytes(cls, raw: bytes, p: int = 14) -> HyperLogLog:
        h = cls(p)
        if len(raw) != h.m:
            raise ValueError("bad register blob")
        h.registers = bytearray(raw)
        return h

    def standard_error(self) -> float:
        return 1.04 / math.sqrt(self.m)
