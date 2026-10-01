"""Redis adapters: latest vehicle state, token buckets, idempotency records, alert pub/sub, OEM quality.

READ CONTRACT with the stream-processing side (written by services/processor; documented in the final report):
  vs:{vin}                hash  tenant, lat, lon, status (PARKED|DRIVING|...), ts_ms, ingest_ts_ms, risk, alerts, oem
  vs:geo:{tenant_id}      geo   member = vin (GEOADD), used for viewport queries (GEOSEARCH BYBOX)
  vs:counts:{tenant_id}   hash  status -> vehicle count
  vs:watch:{tenant_id}    set   VINs the lender flagged (in default / collateral watchlist)
  metrics:freshness_p95_ms string   measured ingest-to-serve p95 (bulk lane)
  flags:model_fallback    string    "1" when the rule baseline replaced the ML model
  alerts:{tenant_id}      pub/sub JSON alert (same shape as the Alert schema)
  oemq:{oem}              hash  records_1h, failed_1h, drift_suspected, drift_detail, quality_score, faults (JSON list)
"""
from __future__ import annotations

import json
import math
import time
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis

from ..domain.ratelimit import Decision

TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local cap = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local d = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(d[1])
local ts = tonumber(d[2])
if tokens == nil then tokens = cap; ts = now end
tokens = math.min(cap, tokens + math.max(0, now - ts) * rate)
local allowed = 0
local retry = 0
if tokens >= cost then tokens = tokens - cost; allowed = 1
elseif rate <= 0 then retry = -1
else retry = (cost - tokens) / rate end
redis.call('HSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
if rate > 0 then redis.call('EXPIRE', key, math.ceil(cap / rate) + 1) else redis.call('PERSIST', key) end
return {allowed, tostring(tokens), tostring(retry)}
"""


def make_redis(url: str) -> aioredis.Redis:
    return aioredis.from_url(url, decode_responses=True, socket_timeout=1.0, socket_connect_timeout=1.0, health_check_interval=30)


class RedisBucketStore:
    def __init__(self, r: aioredis.Redis):
        self._r = r
        self._script = r.register_script(TOKEN_BUCKET_LUA)

    async def take(self, key: str, capacity: int, refill_per_s: float, cost: int = 1) -> Decision:
        allowed, tokens, retry = await self._script(keys=[key], args=[capacity, refill_per_s, cost])
        tokens_f, retry_f = float(tokens), float(retry)
        if refill_per_s <= 0:
            # Zero refill: denied forever (Lua reports retry=-1); mirror bucket_step's inf.
            return Decision(bool(int(allowed)), capacity, int(tokens_f),
                            0 if int(allowed) else math.inf, 0)
        return Decision(bool(int(allowed)), capacity, int(tokens_f), math.ceil(retry_f) if not int(allowed) else 0,
                        math.ceil((capacity - tokens_f) / refill_per_s))


class RedisIdempotencyStore:
    def __init__(self, r: aioredis.Redis):
        self._r = r

    async def begin(self, scope: str, fingerprint: str, ttl_s: int):
        rec = json.dumps({"state": "in_flight", "fp": fingerprint})
        if await self._r.set(scope, rec, nx=True, ex=60):
            return "new", None
        raw = await self._r.get(scope)
        if raw is None:  # expired between SET NX and GET: treat as new
            return await self.begin(scope, fingerprint, ttl_s)
        cur = json.loads(raw)
        if cur.get("fp") != fingerprint:
            return "mismatch", None
        if cur["state"] == "done":
            return "replay", cur["record"]
        return "in_flight", None

    async def complete(self, scope: str, record: dict[str, Any], ttl_s: int) -> None:
        raw = await self._r.get(scope)
        fp = json.loads(raw)["fp"] if raw else ""
        await self._r.set(scope, json.dumps({"state": "done", "fp": fp, "record": record}, default=str), ex=ttl_s)

    async def abort(self, scope: str) -> None:
        await self._r.delete(scope)


class RedisStateStore:
    def __init__(self, r: aioredis.Redis):
        self._r = r

    async def get_states(self, tenant_id: str, vins: list[str]) -> dict[str, dict[str, Any]]:
        pipe = self._r.pipeline(transaction=False)
        for v in vins:
            pipe.hmget(f"vs:{v}", "tenant", "lat", "lon", "status", "ts_ms")
        out: dict[str, dict[str, Any]] = {}
        for v, vals in zip(vins, await pipe.execute(), strict=True):
            tenant, lat, lon, status, ts = vals
            if tenant != tenant_id or lat is None:  # a vs:{vin} key of another tenant is invisible (BOLA defence in depth)
                continue
            out[v] = {"lat": float(lat), "lon": float(lon), "status": status, "ts_ms": int(float(ts or 0))}
        return out

    async def viewport(self, tenant_id, west, south, east, north, limit):
        lon_c, lat_c = (west + east) / 2, (south + north) / 2
        width_m = max(1.0, (east - west) * 111_320 * math.cos(math.radians(lat_c)))
        height_m = max(1.0, (north - south) * 110_540)
        found = await self._r.geosearch(f"vs:geo:{tenant_id}", longitude=lon_c, latitude=lat_c, width=width_m, height=height_m,
                                        unit="m", withcoord=True, count=limit + 1, sort="ASC")
        truncated = len(found) > limit
        found = found[:limit]
        if not found:
            return [], False
        pipe = self._r.pipeline(transaction=False)
        for vin, _ in found:
            pipe.hget(f"vs:{vin}", "status")
        statuses = await pipe.execute()
        return [{"vin": vin, "lat": lat, "lon": lon, "status": st} for (vin, (lon, lat)), st in zip(found, statuses, strict=True)], truncated

    async def status_counts(self, tenant_id: str) -> dict[str, int]:
        return {k: int(v) for k, v in (await self._r.hgetall(f"vs:counts:{tenant_id}")).items()}

    async def freshness_p95_ms(self) -> int:
        return int(float(await self._r.get("metrics:freshness_p95_ms") or 0))

    async def model_fallback_active(self) -> bool:
        return (await self._r.get("flags:model_fallback")) == "1"

    async def is_flagged(self, tenant_id: str, vin: str) -> bool:
        return bool(await self._r.sismember(f"vs:watch:{tenant_id}", vin))

    async def flagged_set(self, tenant_id: str) -> set[str]:
        return set(await self._r.smembers(f"vs:watch:{tenant_id}"))

    async def purge_driver(self, tenant_id: str, driver_id: str) -> int:
        keys = [k async for k in self._r.scan_iter(match=f"driver:{driver_id}*", count=200)]
        return await self._r.delete(*keys) if keys else 0

    async def ping(self) -> None:
        await self._r.ping()


class RedisAlertFeed:
    def __init__(self, r: aioredis.Redis):
        self._r = r

    @asynccontextmanager
    async def subscribe(self, tenant_id: str):
        ps = self._r.pubsub(ignore_subscribe_messages=True)
        await ps.subscribe(f"alerts:{tenant_id}")

        class _Feed:
            async def get(self, timeout: float = 0.0):
                msg = await ps.get_message(ignore_subscribe_messages=True, timeout=timeout)
                return json.loads(msg["data"]) if msg else None

        try:
            yield _Feed()
        finally:
            await ps.aclose()


class RedisQualityStore:
    """OEM quality (Redis hashes written by the normalizer/processor) + DLQ sample/replay (Mongo, see mongo_store)."""

    def __init__(self, r: aioredis.Redis, dlq: Any, oems: tuple[str, ...] = ("OEM_A", "OEM_B", "OEM_C", "OEM_D")):
        self._r, self._dlq, self._oems = r, dlq, oems

    async def oem_quality(self) -> list[dict[str, Any]]:
        out = []
        for oem in self._oems:
            h = await self._r.hgetall(f"oemq:{oem}")
            recs, failed = int(h.get("records_1h", 0)), int(h.get("failed_1h", 0))
            out.append({
                "oem": oem, "records_1h": recs, "failure_rate_1h": round(failed / recs, 4) if recs else 0.0,
                "drift_suspected": h.get("drift_suspected") == "1", "drift_detail": h.get("drift_detail", ""),
                "quality_score": float(h.get("quality_score", 100.0)), "fault_rate_by_sw_version": json.loads(h.get("faults", "[]")),
            })
        return out

    async def dlq_sample(self, oem, reason, limit):
        return await self._dlq.sample(oem, reason, limit)

    async def replay_dlq(self, oem, reason, limit):
        return await self._dlq.replay(oem, reason, limit)


__all__ = ["make_redis", "RedisBucketStore", "RedisIdempotencyStore", "RedisStateStore", "RedisAlertFeed", "RedisQualityStore", "time"]
