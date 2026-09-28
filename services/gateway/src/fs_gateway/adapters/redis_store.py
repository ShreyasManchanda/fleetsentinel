"""Redis exact store for batch dedup: SET key EX ttl [NX], pipelined. Fail-fast timeouts (dedup fails open)."""
from __future__ import annotations

from collections.abc import Sequence

import redis


class RedisExactStore:
    PREFIX = "gw:dedup:"

    def __init__(self, url: str, timeout_s: float = 0.05) -> None:
        self._r = redis.Redis.from_url(url, socket_timeout=timeout_s, socket_connect_timeout=timeout_s)

    def add_many(self, keys: Sequence[str], ttl_s: int) -> None:
        pipe = self._r.pipeline(transaction=False)
        for k in keys:
            pipe.set(self.PREFIX + k, 1, ex=ttl_s)
        pipe.execute()

    def set_nx_many(self, keys: Sequence[str], ttl_s: int) -> list[bool]:
        pipe = self._r.pipeline(transaction=False)
        for k in keys:
            pipe.set(self.PREFIX + k, 1, ex=ttl_s, nx=True)
        return [bool(x) for x in pipe.execute()]