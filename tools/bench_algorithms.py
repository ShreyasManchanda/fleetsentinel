"""One-off micro-benchmarks for docs/algorithms.md (small, laptop-safe)."""
from __future__ import annotations

import random
import time

from fscore.algorithms import segment_naive, segment_pelt
from fscore.algorithms.graph import RoadGraph, astar, dijkstra


def grid(n: int, spacing_km: float = 0.5) -> RoadGraph:
    g = RoadGraph()
    for i in range(n):
        for j in range(n):
            g.add_node((i, j), 12.97 + i * spacing_km / 111.0, 77.59 + j * spacing_km / 111.0)
    blocked = {(i, j) for i in range(5, 15) for j in range(5, 15)}  # city block: forces a detour
    for i in range(n):
        for j in range(n):
            if (i, j) in blocked:
                continue
            if i + 1 < n and (i + 1, j) not in blocked:
                g.add_edge((i, j), (i + 1, j), spacing_km, spacing_km)
            if j + 1 < n and (i, j + 1) not in blocked:
                g.add_edge((i, j), (i, j + 1), spacing_km, spacing_km)
    return g


def main() -> None:
    g = grid(20)
    a = astar(g, (0, 0), (19, 19))
    d = dijkstra(g, (0, 0), (19, 19))
    print(f"astar: cost={a.cost:.3f} expansions={a.expansions}")
    print(f"dijkstra: cost={d.cost:.3f} expansions={d.expansions}")
    assert abs(a.cost - d.cost) < 1e-9

    from fscore.algorithms.graph import nearest_reachable
    goals = {f"c{i}": ((i * 7) % 20, (3 * i + 5) % 20) for i in range(1, 51)}
    for node in goals.values():
        if node not in g.adj:
            g.add_node(node, 12.97, 77.59)
    r = nearest_reachable(g, (0, 0), goals, budget=3.0)
    print(f"nearest_reachable(budget=3km, 50 chargers): found={r.found} goal={r.goal} "
          f"cost={r.cost:.3f} expansions={r.expansions}")

    rng = random.Random(3)
    y: list[float] = []
    for level in (0, 40, 0, 60, 5, 0):
        y += [level + rng.gauss(0, 1) for _ in range(200)]
    t0 = time.perf_counter()
    cp, c1 = segment_pelt(y, 30.0)
    tp = time.perf_counter() - t0
    t0 = time.perf_counter()
    cn, c2 = segment_naive(y, 30.0)
    tn = time.perf_counter() - t0
    print(f"pelt: changepoints={cp} cost={c1:.1f} time={tp * 1000:.1f}ms")
    print(f"naive: changepoints={cn} cost={c2:.1f} time={tn * 1000:.1f}ms")
    assert cp == cn and abs(c1 - c2) < 1e-6


if __name__ == "__main__":
    main()
