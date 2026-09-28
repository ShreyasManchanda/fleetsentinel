"""Union-Find (disjoint sets) with union by size and iterative path compression: O(alpha(n)) amortised per op."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Hashable, Iterable


class UnionFind:
    def __init__(self, items: Iterable[Hashable] = ()) -> None:
        self._parent: dict[Hashable, Hashable] = {}
        self._size: dict[Hashable, int] = {}
        for i in items:
            self.add(i)

    def __len__(self) -> int:
        return len(self._parent)

    def __contains__(self, x: Hashable) -> bool:
        return x in self._parent

    def add(self, x: Hashable) -> None:
        if x not in self._parent:
            self._parent[x] = x
            self._size[x] = 1

    def find(self, x: Hashable) -> Hashable:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:  # path compression
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: Hashable, b: Hashable) -> bool:
        """Merge the sets of a and b (adding them if new). True if they were previously separate."""
        self.add(a)
        self.add(b)
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self._size[ra] < self._size[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        self._size[ra] += self._size[rb]
        return True

    def connected(self, a: Hashable, b: Hashable) -> bool:
        return a in self._parent and b in self._parent and self.find(a) == self.find(b)

    def size_of(self, x: Hashable) -> int:
        return self._size[self.find(x)]

    def components(self) -> list[list[Hashable]]:
        groups: dict[Hashable, list[Hashable]] = defaultdict(list)
        for x in self._parent:
            groups[self.find(x)].append(x)
        return list(groups.values())
