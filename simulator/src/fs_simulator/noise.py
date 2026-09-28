"""Transport-level noise: ~2% out-of-order records (delivered late), ~1% duplicate records; whole-batch retries are done by
the publisher. Everything is counted so ground truth and the reconciliation report can account for it."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .behaviour import Emission


def concat_emissions(parts: list[Emission]) -> Emission:
    if len(parts) == 1:
        return parts[0]
    return Emission(**{k: np.concatenate([getattr(p, k) for p in parts]) for k in Emission.__dataclass_fields__})


def empty_like(em: Emission) -> Emission:
    return em.select(np.zeros(len(em), dtype=bool))


@dataclass
class NoiseStats:
    ooo_records: int = 0
    dup_records: int = 0
    batch_retries: int = 0
    batch_retry_records: int = 0
    per_kind: dict[str, int] = field(default_factory=dict)


class NoiseInjector:
    def __init__(self, rng: np.random.Generator, ooo_p: float = 0.02, dup_p: float = 0.01, delay_s: tuple[float, float] = (2.0, 30.0)):
        self.rng, self.ooo_p, self.dup_p, self.delay_s = rng, ooo_p, dup_p, delay_s
        self.held: Emission | None = None
        self.held_rel = np.zeros(0)
        self.held_kind = np.zeros(0, dtype=np.int8)      # 1 = late original, 2 = duplicate copy
        self.stats = NoiseStats()

    def apply(self, em: Emission, now_s: float) -> tuple[Emission, np.ndarray, np.ndarray]:
        """Returns (emission, is_dup, is_late) for the records to send this tick."""
        n = len(em)
        rng = self.rng
        ooo = rng.random(n) < self.ooo_p
        dup = (rng.random(n) < self.dup_p) & ~ooo
        keep = em.select(~ooo)
        new_held = [em.select(ooo)]
        rel = [now_s + rng.uniform(*self.delay_s, int(ooo.sum()))]
        kind = [np.full(int(ooo.sum()), 1, dtype=np.int8)]
        if dup.any():
            new_held.append(em.select(dup))
            rel.append(now_s + rng.uniform(0.0, 5.0, int(dup.sum())))
            kind.append(np.full(int(dup.sum()), 2, dtype=np.int8))
        parts = ([self.held] if self.held is not None else []) + new_held
        rels = np.concatenate([self.held_rel] + rel)
        kinds = np.concatenate([self.held_kind] + kind)
        allh = concat_emissions(parts) if parts else empty_like(em)
        due = rels <= now_s
        rel_em = allh.select(due)
        rel_kind = kinds[due]
        self.held, self.held_rel, self.held_kind = allh.select(~due), rels[~due], kinds[~due]
        out = concat_emissions([keep, rel_em])
        is_dup = np.concatenate([np.zeros(len(keep), dtype=bool), rel_kind == 2])
        is_late = np.concatenate([np.zeros(len(keep), dtype=bool), rel_kind == 1])
        self.stats.ooo_records += int(ooo.sum())
        self.stats.dup_records += int(dup.sum())
        return out, is_dup, is_late

    def flush(self) -> Emission | None:
        """Release everything still held (end of run)."""
        out, self.held = self.held, None
        self.held_rel = np.zeros(0)
        self.held_kind = np.zeros(0, dtype=np.int8)
        return out
