"""Road graph, A* with an admissible haversine heuristic, and a Dijkstra baseline.

Edges carry a physical length (km) and a COST (kWh for the SoC-constrained use). The heuristic is
``haversine_km * min_cost_per_km`` where ``min_cost_per_km`` is the cheapest cost/length ratio of any edge, so it never
overestimates (edge length >= straight-line distance is enforced in ``add_edge``) and it is consistent. Because the
constraint "arrive with SoC >= reserve" is a budget on the same cost that is minimised, the cheapest path is feasible
iff any path is: pruning ``g + h > budget`` is exact, not a heuristic.

A* worst case O((V+E) log V) like Dijkstra, but expands far fewer nodes when the heuristic is informative. Both
searches report ``expansions`` so the write-up can compare them.
"""
from __future__ import annotations

import heapq
import math
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, field

from .geo import haversine_km


@dataclass(slots=True)
class SearchResult:
    found: bool
    cost: float = math.inf
    path: list[Hashable] = field(default_factory=list)
    expansions: int = 0
    goal: Hashable | None = None


class RoadGraph:
    def __init__(self) -> None:
        self.pos: dict[Hashable, tuple[float, float]] = {}
        self.adj: dict[Hashable, list[tuple[Hashable, float]]] = {}   # node -> [(neighbour, cost)]
        self.min_cost_per_km = math.inf

    def add_node(self, n: Hashable, lat: float, lon: float) -> None:
        self.pos[n] = (lat, lon)
        self.adj.setdefault(n, [])

    def add_edge(self, u: Hashable, v: Hashable, length_km: float, cost: float, bidirectional: bool = True) -> None:
        straight = haversine_km(*self.pos[u], *self.pos[v])
        length_km = max(length_km, straight)  # keep the heuristic admissible
        if cost < 0:
            raise ValueError("negative edge cost")
        if length_km > 0:
            self.min_cost_per_km = min(self.min_cost_per_km, cost / length_km)
        self.adj[u].append((v, cost))
        if bidirectional:
            self.adj[v].append((u, cost))

    def heuristic(self, n: Hashable, goal: Hashable) -> float:
        if math.isinf(self.min_cost_per_km):
            return 0.0
        return haversine_km(*self.pos[n], *self.pos[goal]) * self.min_cost_per_km


def _search(
    g: RoadGraph,
    start: Hashable,
    is_goal: Callable[[Hashable], bool],
    h: Callable[[Hashable], float],
    budget: float,
) -> SearchResult:
    if start not in g.adj:
        return SearchResult(False)
    best: dict[Hashable, float] = {start: 0.0}
    parent: dict[Hashable, Hashable] = {}
    counter = 0
    heap: list[tuple[float, int, float, Hashable]] = [(h(start), counter, 0.0, start)]
    closed: set[Hashable] = set()
    expansions = 0
    while heap:
        _f, _, gc, u = heapq.heappop(heap)
        if u in closed or gc > best.get(u, math.inf):
            continue
        closed.add(u)
        expansions += 1
        if is_goal(u):
            path = [u]
            while path[-1] in parent:
                path.append(parent[path[-1]])
            path.reverse()
            return SearchResult(True, gc, path, expansions, u)
        for v, c in g.adj[u]:
            ng = gc + c
            if ng > budget or ng >= best.get(v, math.inf):
                continue
            nf = ng + h(v)
            if nf > budget:
                continue
            best[v] = ng
            parent[v] = u
            counter += 1
            heapq.heappush(heap, (nf, counter, ng, v))
    return SearchResult(False, expansions=expansions)


def astar(g: RoadGraph, start: Hashable, goal: Hashable, budget: float = math.inf) -> SearchResult:
    """Cheapest path start->goal with total cost <= budget, using the haversine heuristic."""
    if goal not in g.adj:
        return SearchResult(False)
    return _search(g, start, lambda n: n == goal, lambda n: g.heuristic(n, goal), budget)


def dijkstra(g: RoadGraph, start: Hashable, goal: Hashable, budget: float = math.inf) -> SearchResult:
    """Baseline: same search with h = 0."""
    if goal not in g.adj:
        return SearchResult(False)
    return _search(g, start, lambda n: n == goal, lambda n: 0.0, budget)


def nearest_reachable(
    g: RoadGraph,
    start: Hashable,
    goals: dict[Hashable, Hashable],
    budget: float,
    use_heuristic: bool = True,
) -> SearchResult:
    """Cheapest goal reachable within ``budget``. ``goals`` maps goal id -> graph node; result.goal is the id.

    Multi-goal A*: h(n) = min over candidate goals of haversine(n, goal) * min_cost_per_km (still admissible and
    consistent). Goals whose straight-line cost already exceeds the budget are discarded up front.
    """
    node_to_ids: dict[Hashable, list[Hashable]] = {}
    for gid, node in goals.items():
        node_to_ids.setdefault(node, []).append(gid)
    if start not in g.adj:
        return SearchResult(False)
    rate = 0.0 if math.isinf(g.min_cost_per_km) else g.min_cost_per_km
    slat, slon = g.pos[start]
    cand = [n for n in node_to_ids if haversine_km(slat, slon, *g.pos[n]) * rate <= budget]
    if not cand:
        return SearchResult(False)
    cand_pos = [g.pos[n] for n in cand]
    cand_set = set(cand)

    def h(n: Hashable) -> float:
        if not use_heuristic:
            return 0.0
        la, lo = g.pos[n]
        return min(haversine_km(la, lo, cl, co) for cl, co in cand_pos) * rate

    res = _search(g, start, lambda n: n in cand_set, h, budget)
    if res.found:
        res.goal = sorted(node_to_ids[res.goal], key=str)[0]
    return res
