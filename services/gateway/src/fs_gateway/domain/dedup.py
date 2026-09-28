"""Batch de-duplication: Bloom filter as a NEGATIVE filter only + exact TTL store to confirm.

* Bloom says "not seen"  -> trusted: the batch is new (no exact lookup needed; the key is recorded fire-and-forget).
* Bloom says "maybe"     -> confirmed with an atomic ``SET NX`` on the exact store. A Bloom false positive therefore
  costs one extra store call and can NEVER drop a valid batch.
* After a restart the Bloom is empty, so during ``cold_exact_s`` every key goes through the exact store.
* If the exact store is down the deduper FAILS OPEN (treats the batch as new): duplicates are harmless because the
  processor does exact per-VIN anti-replay dedup; losing data would not be.
"""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Sequence
from typing import Protocol

from fscore.sketches import RotatingBloom

from ..metrics import GatewayMetrics


class ExactStore(Protocol):
    def add_many(self, keys: Sequence[str], ttl_s: int) -> None:
        """Record keys unconditionally (no reply needed)."""

    def set_nx_many(self, keys: Sequence[str], ttl_s: int) -> list[bool]:
        """Atomically record keys; True for each key that was NOT present before."""


def batch_key(oem: str, kind: str, payload: bytes) -> str:
    """Whole-batch identity: an exact retry of the same payload from the same OEM/channel has the same key."""
    h = hashlib.blake2b(digest_size=16)
    h.update(f"{oem}|{kind}|".encode())
    h.update(payload)
    return h.hexdigest()


class BatchDeduper:
    def __init__(self, store: ExactStore, bloom: RotatingBloom, ttl_s: int = 3600, cold_exact_s: float = 300.0,
                 metrics: GatewayMetrics | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.store, self.bloom, self.ttl_s = store, bloom, ttl_s
        self.metrics = metrics or GatewayMetrics()
        self._clock = clock
        self._cold_until = clock() + cold_exact_s

    def filter(self, keys: Sequence[str]) -> list[bool]:
        """Return keep[i] (True = new batch, forward it; False = duplicate, drop)."""
        cold = self._clock() < self._cold_until
        keep = [True] * len(keys)
        local: set[str] = set()
        fresh: list[int] = []      # Bloom-proven new
        maybe: list[int] = []      # need the exact store
        for i, k in enumerate(keys):
            if k in local:
                keep[i] = False    # duplicate inside this very chunk
                continue
            local.add(k)
            (maybe if (cold or k in self.bloom) else fresh).append(i)
        try:
            if fresh:
                self.store.add_many([keys[i] for i in fresh], self.ttl_s)
            if maybe:
                res = self.store.set_nx_many([keys[i] for i in maybe], self.ttl_s)
                for i, is_new in zip(maybe, res, strict=True):
                    keep[i] = is_new
        except Exception:  # noqa: BLE001 - fail open by design
            self.metrics.store_errors.inc()
        for i in fresh:
            self.bloom.add(keys[i])
        for i in maybe:
            if keep[i]:
                self.bloom.add(keys[i])
        self.metrics.bloom_negative.inc(len(fresh))
        self.metrics.bloom_maybe.inc(len(maybe))
        dups = keep.count(False)
        if dups:
            self.metrics.duplicates.inc(dups)
        return keep