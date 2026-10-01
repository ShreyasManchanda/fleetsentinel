"""Redis/Postgres/Mongo adapters (integration-tested; excluded from the gate).

- Redis: per-partition checkpoints via one MULTI/EXEC (aggregates + dedup + last_offset
  written atomically, BEFORE the Kafka offset is committed) plus latest VehicleState.
- Postgres: alerts with ``ON CONFLICT DO NOTHING`` on the deterministic alert_id.
- Mongo: vehicle digital-twin upserts (throttled by the caller).
"""
from __future__ import annotations

import orjson
import structlog

log = structlog.get_logger("fs_processor.stores")


class RedisCheckpointStore:
    """Atomic per-partition checkpoint: SET state + last_offset in one MULTI/EXEC."""

    def __init__(self, url: str, prefix: str = "fs:proc") -> None:
        import redis

        self._r = redis.Redis.from_url(url)
        self._prefix = prefix

    def _key(self, partition: int) -> str:
        return f"{self._prefix}:ckpt:{partition}"

    def save(self, partition: int, state: dict) -> None:
        pipe = self._r.pipeline(transaction=True)
        pipe.set(self._key(partition), orjson.dumps(state))
        pipe.execute()  # MULTI/EXEC: aggregates + dedup windows + last_offset land together

    def load(self, partition: int) -> dict | None:
        raw = self._r.get(self._key(partition))
        return None if raw is None else orjson.loads(raw)

    def put_state(self, vin: str, value: bytes) -> None:
        self._r.set(f"{self._prefix}:state:{vin}", value, ex=300)

    def commit(self, partition: int, offset: int) -> None:
        self._r.set(f"{self._prefix}:committed:{partition}", offset)


class PostgresAlertStore:
    """Idempotent alert sink: deterministic ids + ON CONFLICT DO NOTHING."""

    def __init__(self, dsn: str, tenant_sql: str = "SELECT set_config('app.tenant_id', %s, true)") -> None:
        from psycopg_pool import ConnectionPool

        self._pool = ConnectionPool(dsn, open=True)
        self._tenant_sql = tenant_sql

    def insert_alerts(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        with self._pool.connection() as conn, conn.cursor() as cur:
            n = 0
            for r in rows:
                cur.execute(self._tenant_sql, (r["tenant_id"],))
                cur.execute(
                    """INSERT INTO alert
                       (id, tenant_id, vehicle_id, rule_id, rule_code, severity, status, occurrences,
                        impact_at_creation, opened_at)
                       VALUES (%s, %s, (SELECT id FROM vehicle WHERE vin = %s),
                               (SELECT id FROM alert_rule WHERE code = %s AND tenant_id = %s LIMIT 1),
                               %s, %s, 'open', 1, %s, now())
                       ON CONFLICT (id) DO NOTHING""",
                    (r["alert_id"], r["tenant_id"], r["vin"], r["rule"], r["tenant_id"], r["rule"], r["severity"],
                     r.get("impact_usd", 0)),
                )
                n += cur.rowcount
            conn.commit()
            return n


class MongoTwinStore:
    """Vehicle digital-twin upserts (one document per vehicle)."""

    def __init__(self, uri: str, db: str = "fleetsentinel") -> None:
        import pymongo

        self._col = pymongo.MongoClient(uri)[db]["twins"]

    def upsert(self, vin: str, doc: dict) -> None:
        self._col.update_one({"vin": vin}, {"$set": doc}, upsert=True)


__all__ = ["MongoTwinStore", "PostgresAlertStore", "RedisCheckpointStore"]
