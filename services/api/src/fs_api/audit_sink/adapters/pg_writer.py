"""fs_worker writer for audit_log (psycopg, synchronous). The BEFORE INSERT trigger computes the hash chain."""
from __future__ import annotations

import json
from typing import Any

import psycopg

INSERT_SQL = """
INSERT INTO audit_log (id, ts, tenant_id, actor_id, actor_type, action, resource_type, resource_ids, purpose, decision, reason, latency_ms, attributes)
SELECT %(id)s, %(ts)s, %(tenant_id)s, %(actor_id)s, %(actor_type)s, %(action)s, %(resource_type)s, %(resource_ids)s, %(purpose)s,
       %(decision)s, %(reason)s, %(latency_ms)s, %(attributes)s::jsonb
WHERE NOT EXISTS (
    SELECT 1 FROM audit_log WHERE id = %(id)s AND ts = %(ts)s AND tenant_id IS NOT DISTINCT FROM %(tenant_id)s
)
"""


class PgAuditWriter:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn: psycopg.Connection | None = None

    def _connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._dsn, autocommit=False)
        return self._conn

    def write_batch(self, rows: list[dict[str, Any]]) -> int:
        conn = self._connection()
        inserted = 0
        try:
            with conn.transaction(), conn.cursor() as cur:
                for r in rows:
                    cur.execute(INSERT_SQL, {**r, "attributes": json.dumps(r["attributes"])})
                    inserted += cur.rowcount
        except Exception:
            self._conn = None  # force a reconnect on the next attempt
            raise
        return inserted
