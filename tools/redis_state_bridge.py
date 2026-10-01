"""Stopgap: project the processor's `fs:proc:state:{vin}` (VehicleState protobuf) into the `vs:*` layout the API reads
(see services/api/.../redis_store.py header). Run alongside the bulk processor for a live demo:
    uv run --frozen python tools/redis_state_bridge.py
"""
from __future__ import annotations

import os
import time

import redis
from fleetsentinel.v1 import state_pb2

R = redis.Redis.from_url(os.environ.get("FS_REDIS", "redis://localhost:6379/0"))
PREFIX = b"fs:proc:state:"


def sweep() -> int:
    counts: dict[str, dict[str, int]] = {}
    lags: list[int] = []
    n = 0
    now_ms = int(time.time() * 1000)
    keys = list(R.scan_iter(match=PREFIX + b"*", count=2000))
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        pipe = R.pipeline(transaction=False)
        for k in chunk:
            pipe.get(k)
        out = R.pipeline(transaction=False)
        for raw in pipe.execute():
            if not raw:
                continue
            s = state_pb2.VehicleState()
            try:
                s.ParseFromString(raw)
            except Exception:  # noqa: BLE001, S112 - skip undecodable value
                continue
            if not s.tenant_id or s.tenant_id == "unknown" or not (s.lat or s.lon):
                continue
            status = state_pb2.VehicleStatus.Name(s.status).removeprefix("VEHICLE_STATUS_").removeprefix("STATUS_")
            out.hset(f"vs:{s.vin}", mapping={"tenant": s.tenant_id, "lat": s.lat, "lon": s.lon, "status": status,
                                              "ts_ms": s.ts_ms, "ingest_ts_ms": s.ingest_ts_ms, "risk": s.risk_score,
                                              "alerts": s.open_alerts, "oem": s.oem})
            out.geoadd(f"vs:geo:{s.tenant_id}", (s.lon, s.lat, s.vin))
            counts.setdefault(s.tenant_id, {}).setdefault(status, 0)
            counts[s.tenant_id][status] += 1
            if s.ingest_ts_ms and now_ms - s.ingest_ts_ms < 120_000:  # only live states; ignore stale replays
                lags.append(max(0, now_ms - s.ingest_ts_ms))
            n += 1
        out.execute()
    for t, c in counts.items():
        R.delete(f"vs:counts:{t}")
        R.hset(f"vs:counts:{t}", mapping=c)
    if lags:
        lags.sort()
        R.set("metrics:freshness_p95_ms", lags[int(len(lags) * 0.95) - 1] if len(lags) > 1 else lags[0])
    return n


if __name__ == "__main__":
    while True:
        print("bridged", sweep(), flush=True)
        time.sleep(3)
