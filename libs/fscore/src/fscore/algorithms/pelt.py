"""Penalised changepoint segmentation (optimal partitioning), PELT-pruned, plus the naive O(n^2) baseline.

Model: piecewise-constant mean, segment cost = sum of squared deviations from the segment mean (computed in O(1)
from prefix sums). Objective: ``sum(cost(segment)) + penalty * n_segments``.

* ``segment_naive``: exact DP over all previous changepoints, O(n^2).
* ``segment_pelt``: Killick et al. (2012) pruning. A candidate ``s`` is dropped forever once
  ``F[s] + cost(s, t) > F[t]`` (valid because the cost satisfies the K=0 condition). Expected O(n) when the number
  of changepoints grows linearly with n; worst case O(n^2). Returns the SAME optimum as the naive baseline.
"""
from __future__ import annotations

import math
from collections.abc import Sequence


def _prefix(y: Sequence[float]) -> tuple[list[float], list[float]]:
    s1 = [0.0]
    s2 = [0.0]
    a = b = 0.0
    for v in y:
        a += v
        b += v * v
        s1.append(a)
        s2.append(b)
    return s1, s2


def _cost(s1: list[float], s2: list[float], s: int, t: int) -> float:
    n = t - s
    d = s1[t] - s1[s]
    c = (s2[t] - s2[s]) - d * d / n
    return c if c > 0.0 else 0.0


def _backtrack(last: list[int], n: int) -> list[int]:
    cps: list[int] = []
    t = n
    while t > 0:
        s = last[t]
        if s > 0:
            cps.append(s)
        t = s
    cps.reverse()
    return cps


def segment_naive(y: Sequence[float], penalty: float) -> tuple[list[int], float]:
    """Return (changepoint indexes, total penalised cost). Segments are [0,c1), [c1,c2), ..."""
    n = len(y)
    if n == 0:
        return [], 0.0
    s1, s2 = _prefix(y)
    f = [0.0] * (n + 1)
    last = [0] * (n + 1)
    f[0] = -penalty
    for t in range(1, n + 1):
        best = math.inf
        arg = 0
        for s in range(t):
            v = f[s] + _cost(s1, s2, s, t) + penalty
            if v < best:
                best, arg = v, s
        f[t], last[t] = best, arg
    return _backtrack(last, n), f[n] + penalty


def segment_pelt(y: Sequence[float], penalty: float) -> tuple[list[int], float]:
    """PELT with pruning; identical optimum to ``segment_naive``."""
    n = len(y)
    if n == 0:
        return [], 0.0
    s1, s2 = _prefix(y)
    f = [0.0] * (n + 1)
    last = [0] * (n + 1)
    f[0] = -penalty
    cand = [0]
    for t in range(1, n + 1):
        best = math.inf
        arg = 0
        vals = []
        for s in cand:
            base = f[s] + _cost(s1, s2, s, t)
            vals.append(base)
            if base + penalty < best:
                best, arg = base + penalty, s
        f[t], last[t] = best, arg
        cand = [s for s, v in zip(cand, vals, strict=True) if v <= f[t]]
        cand.append(t)
    return _backtrack(last, n), f[n] + penalty


def segments(changepoints: list[int], n: int) -> list[tuple[int, int]]:
    """Convert changepoints to [start, end) index pairs."""
    bounds = [0, *changepoints, n]
    return [(a, b) for a, b in zip(bounds, bounds[1:], strict=False) if b > a]


def segment_trips(
    speeds_kmh: Sequence[float], penalty: float | None = None, moving_kmh: float = 3.0
) -> list[tuple[int, int, bool]]:
    """Authoritative batch trip/stop segmentation of a speed series: PELT on speed, then label by segment mean.

    Returns [(start_idx, end_idx_exclusive, is_moving)] with adjacent same-label segments merged.
    """
    n = len(speeds_kmh)
    if n == 0:
        return []
    if penalty is None:
        mean = sum(speeds_kmh) / n
        var = sum((v - mean) ** 2 for v in speeds_kmh) / n
        penalty = max(0.2 * var * math.log(max(n, 2)), 25.0)
    cps, _ = segment_pelt(speeds_kmh, penalty)
    out: list[tuple[int, int, bool]] = []
    for a, b in segments(cps, n):
        moving = sum(speeds_kmh[a:b]) / (b - a) >= moving_kmh
        if out and out[-1][2] == moving:
            out[-1] = (out[-1][0], b, moving)
        else:
            out.append((a, b, moving))
    return out
