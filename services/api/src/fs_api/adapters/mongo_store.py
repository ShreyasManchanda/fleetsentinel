"""MongoDB adapters: digital twins (read whole by Vehicle 360) and the DLQ quarantine (sample + replay)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pymongo import AsyncMongoClient


def make_mongo(url: str) -> AsyncMongoClient:
    return AsyncMongoClient(url, serverSelectionTimeoutMS=1500, connectTimeoutMS=1500, tz_aware=True)


class MongoTwinStore:
    """Collection `twins`: one document per vehicle {_id: vin, tenant_id, ...schema-variable twin...}. The tenant filter is
    applied in the query itself (Mongo has no RLS), so a VIN of another tenant simply is not found."""

    def __init__(self, client: AsyncMongoClient, db: str = "fleetsentinel"):
        self._db = client[db]
        self._client = client

    async def get(self, tenant_id: str, vin: str) -> dict[str, Any] | None:
        return await self._db.twins.find_one({"_id": vin, "tenant_id": tenant_id})

    async def purge_driver(self, tenant_id: str, driver_id: str) -> int:
        r = await self._db.twins.update_many({"tenant_id": tenant_id, "driver_id": driver_id}, {"$unset": {"driver_id": "", "driver": ""}})
        return r.modified_count

    async def ping(self) -> None:
        await self._client.admin.command("ping")


class MongoDlq:
    """Collection `dlq`: {oem, error_reason, ts, payload, batch_id?, replayed?}. Replay re-publishes payloads to ingest.raw.v1."""

    def __init__(self, client: AsyncMongoClient, producer: Any | None = None, db: str = "fleetsentinel"):
        self._c = client[db].dlq
        self._producer = producer

    async def sample(self, oem: str | None, reason: str | None, limit: int) -> list[dict[str, Any]]:
        flt: dict[str, Any] = {}
        if oem:
            flt["oem"] = oem
        if reason:
            flt["error_reason"] = reason
        cur = self._c.find(flt, {"_id": 0, "oem": 1, "error_reason": 1, "ts": 1, "payload": 1}).sort("ts", -1).limit(limit)
        return [d async for d in cur]

    async def replay(self, oem: str, reason: str | None, limit: int | None) -> dict[str, Any]:
        flt: dict[str, Any] = {"oem": oem, "replayed": {"$ne": True}}
        if reason:
            flt["error_reason"] = reason
        n = 0
        async for doc in self._c.find(flt).limit(limit or 1_000_000):
            if self._producer is not None:
                self._producer.produce("ingest.raw.v1", key=f"{oem}:dlq".encode(), value=_as_bytes(doc.get("payload")),
                                       headers=[("oem", oem.encode()), ("replay", b"1")])
            await self._c.update_one({"_id": doc["_id"]}, {"$set": {"replayed": True, "replayed_at": datetime.now(UTC)}})
            n += 1
        if self._producer is not None:
            self._producer.flush(5.0)
        return {"job_id": str(uuid.uuid4()), "records": n}


def _as_bytes(payload: Any) -> bytes:
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    import json

    return payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
