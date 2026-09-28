"""Postgres adapter: one request = one transaction with `set_config('app.tenant_id', :tenant, true)` executed FIRST, then hand-written
SQL (see pg_sql.py). Connects as fs_app, so Row-Level Security applies to every statement."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from ..errors import unprocessable
from ..ports import Repos, Row
from . import pg_sql as q


def make_engine(url: str, pool_size: int = 10) -> AsyncEngine:
    return create_async_engine(url, pool_size=pool_size, max_overflow=pool_size, pool_pre_ping=True, pool_timeout=3, pool_recycle=1800)


def _row(r: Any) -> Row:
    return dict(r._mapping)


async def _all(conn: AsyncConnection, sql: str, params: dict[str, Any] | None = None) -> list[Row]:
    return [_row(r) for r in (await conn.execute(text(sql), params or {})).fetchall()]


async def _one(conn: AsyncConnection, sql: str, params: dict[str, Any] | None = None) -> Row | None:
    r = (await conn.execute(text(sql), params or {})).first()
    return _row(r) if r is not None else None


def _j(v: Any) -> str:
    return json.dumps(v, default=str, separators=(",", ":"))


class PgTenantDb:
    def __init__(self, engine: AsyncEngine, privileged_engine: AsyncEngine | None = None, statement_timeout_ms: int = 5000):
        self._engine, self._priv, self._timeout = engine, privileged_engine, statement_timeout_ms

    @asynccontextmanager
    async def tx(self, tenant_id: str) -> AsyncIterator[Repos]:
        async with self._engine.connect() as conn, conn.begin():
            # transaction-local (third arg true): cannot leak to the next request on a pooled connection
            await conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            await conn.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(self._timeout)})
            yield Repos(
                vehicles=PgVehicles(conn), alerts=PgAlerts(conn), drivers=PgDrivers(conn), actions=PgActions(conn),
                mappings=PgMappings(conn), audit=PgAudit(conn), dsr=PgDsr(conn, tenant_id, self._priv), reports=PgReports(conn),
            )

    async def ping(self) -> None:
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT 1"))


class PgVehicles:
    def __init__(self, conn: AsyncConnection):
        self.c = conn

    async def list_page(self, *, fleet_id, min_risk, vin_prefix, sort, after, limit) -> list[Row]:
        p: dict[str, Any] = {"limit": limit}
        if fleet_id:
            p["fleet_id"] = fleet_id
        if min_risk is not None:
            p["min_risk"] = min_risk
        if vin_prefix:
            p["prefix"] = vin_prefix + "%"
        if after:
            if sort == "risk_desc":
                p["k_risk"], p["k_vin"] = after[0], after[1]
            else:
                p["k_vin"] = after[0]
        return await _all(self.c, q.vehicle_list_sql(bool(fleet_id), min_risk is not None, bool(vin_prefix), bool(after), sort), p)

    async def get(self, vin: str) -> Row | None:
        return await _one(self.c, q.VEHICLE_GET, {"vin": vin})

    async def _exists(self, vin: str) -> bool:
        return await _one(self.c, q.VEHICLE_ID, {"vin": vin}) is not None

    async def trips(self, vin, after, limit) -> list[Row] | None:
        if not await self._exists(vin):
            return None
        p: dict[str, Any] = {"vin": vin, "limit": limit}
        if after:
            p["k_ts"], p["k_id"] = after[0], after[1]
        return await _all(self.c, q.trips_sql(bool(after)), p)

    async def recent_trips(self, vin, n) -> list[Row]:
        return await _all(self.c, q.trips_sql(False), {"vin": vin, "limit": n})

    async def recent_alerts(self, vin, n) -> list[Row]:
        return await _all(self.c, q.VEHICLE_RECENT_ALERTS, {"vin": vin, "n": n})

    async def latest_risk(self, vin) -> Row | None:
        return await _one(self.c, q.LATEST_RISK, {"vin": vin})

    async def lookalikes(self, vin, k) -> list[Row] | None:
        if not await self._exists(vin):
            return None
        return await _all(self.c, q.LOOKALIKES, {"vin": vin, "k": k, "k_over": k * 4})

    async def fleet_summary(self, fleet_id, risk_threshold) -> Row:
        r = await _one(self.c, q.FLEET_SUMMARY, {"fleet_id": fleet_id, "thr": risk_threshold})
        return r or {"vehicles_total": 0, "at_risk_vehicles": 0, "open_critical_alerts": 0, "leaking_usd_today": 0, "savings_opportunity_usd_week": 0}


class PgAlerts:
    def __init__(self, conn: AsyncConnection):
        self.c = conn

    async def list_page(self, *, statuses, severity, rule, vin, since, after, limit) -> list[Row]:
        p: dict[str, Any] = {"limit": limit}
        if severity:
            p["severity"] = severity
        if rule:
            p["rule"] = rule
        if vin:
            p["vin"] = vin
        if since:
            p["since"] = since
        if after:
            p["k_ts"], p["k_id"] = after[0], after[1]
        return await _all(self.c, q.alert_list_sql(statuses, bool(severity), bool(rule), bool(vin), bool(since), bool(after)), p)

    async def get(self, alert_id) -> Row | None:
        return await _one(self.c, q.ALERT_GET, {"id": alert_id})

    async def set_status(self, alert_id, status, reviewer_sub) -> Row | None:
        if status == "ack":
            r = await _one(self.c, q.ALERT_ACK, {"id": alert_id, "sub": reviewer_sub})
        else:
            r = await _one(self.c, q.ALERT_RESOLVE, {"id": alert_id})
        return await self.get(alert_id) if r else None


class PgDrivers:
    def __init__(self, conn: AsyncConnection):
        self.c = conn

    async def list_page(self, *, sort, fleet_id, after, limit) -> list[Row]:
        p: dict[str, Any] = {"limit": limit}
        if fleet_id:
            p["fleet_id"] = fleet_id
        if after:
            p["k_score"], p["k_id"] = after[0], after[1]
        return await _all(self.c, q.driver_list_sql(bool(fleet_id), bool(after), sort), p)

    async def get(self, driver_id) -> Row | None:
        r = await _one(self.c, q.DRIVER_DETAIL, {"id": driver_id})
        if r is None:
            return None
        keys = ("harsh_brake", "harsh_accel", "overspeed_min", "night_share", "idle_share")
        r["per_100km"] = {k: (float(r[k]) if r.get(k) is not None else None) for k in keys}
        r["fleet_p50"] = {k: (float(r["p_" + k]) if r.get("p_" + k) is not None else 0.0) for k in keys}
        return r


class PgActions:
    def __init__(self, conn: AsyncConnection):
        self.c = conn

    async def list_page(self, *, status, after, limit) -> list[Row]:
        p: dict[str, Any] = {"limit": limit}
        if status:
            p["status"] = status
        if after:
            p["k_ts"], p["k_id"] = after[0], after[1]
        return await _all(self.c, q.action_list_sql(bool(status), bool(after)), p)

    async def create(self, *, proposal, rationale, expected_impact_usd, alert_id, idempotency_key) -> Row:
        if alert_id and await _one(self.c, q.ALERT_EXISTS, {"id": alert_id}) is None:
            raise unprocessable("alert_id does not exist in this tenant", "unknown-alert")
        r = await _one(self.c, q.ACTION_CREATE, {"alert_id": alert_id, "proposal": _j(proposal), "rationale": rationale,
                                                 "impact": expected_impact_usd, "key": idempotency_key})
        assert r is not None
        return await self.get(str(r["id"]))  # type: ignore[return-value]

    async def get(self, action_id) -> Row | None:
        return await _one(self.c, q.ACTION_GET, {"id": action_id})

    async def decide(self, action_id, status, reviewer_sub, reason) -> Row | None:
        r = await _one(self.c, q.ACTION_DECIDE, {"id": action_id, "status": status, "sub": reviewer_sub, "reason": reason})
        if r is None:
            return None
        if status == "approved":
            await self.c.execute(text(q.WORK_ORDER_CREATE), {"id": action_id})
        return await self.get(action_id)


class PgMappings:
    def __init__(self, conn: AsyncConnection):
        self.c = conn

    async def list_all(self) -> list[Row]:
        return await _all(self.c, q.MAPPING_LIST)

    async def next_version(self, oem) -> int | None:
        r = await _one(self.c, q.MAPPING_NEXT_VERSION, {"oem": oem})
        return int(r["next_version"]) if r else None

    async def create_draft(self, oem, version, document, created_by) -> Row | None:
        r = await _one(self.c, q.MAPPING_CREATE, {"oem": oem, "version": version, "document": _j(document), "created_by": created_by})
        return await self.get(oem, version) if r else None

    async def get(self, oem, version) -> Row | None:
        return await _one(self.c, q.MAPPING_GET, {"oem": oem, "version": version})

    async def set_status(self, oem, version, status) -> Row:
        assert status == "approved"
        await self.c.execute(text(q.MAPPING_APPROVE), {"oem": oem, "version": version})
        return await self.get(oem, version)  # type: ignore[return-value]

    async def activate(self, oem, version) -> Row:
        await self.c.execute(text(q.MAPPING_RETIRE_ACTIVE), {"oem": oem})
        await self.c.execute(text(q.MAPPING_ACTIVATE), {"oem": oem, "version": version})
        return await self.get(oem, version)  # type: ignore[return-value]

    async def active_versions(self) -> dict[str, int]:
        return {r["oem"]: int(r["version"]) for r in await _all(self.c, q.MAPPING_ACTIVE_VERSIONS)}


class PgAudit:
    def __init__(self, conn: AsyncConnection):
        self.c = conn

    async def list_page(self, *, actor_id, action, decision, after, limit) -> list[Row]:
        p: dict[str, Any] = {"limit": limit}
        if actor_id:
            p["actor_id"] = actor_id
        if action:
            p["action"] = action
        if decision:
            p["decision"] = decision
        if after:
            p["k_seq"] = after[0]
        return await _all(self.c, q.audit_list_sql(bool(actor_id), bool(action), bool(decision), bool(after)), p)

    async def verify(self, from_seq, to_seq) -> Row:
        hi = to_seq if to_seq is not None else 9223372036854775807
        r = await _one(self.c, q.AUDIT_VERIFY, {"from_seq": from_seq, "to_seq": hi}) or {"ok": True, "checked": 0, "first_bad_seq": None}
        head = await _one(self.c, q.AUDIT_HEAD, {"from_seq": from_seq, "to_seq": hi})
        anchor = await _one(self.c, q.AUDIT_ANCHOR)
        return {**r, "head_hash": head["head_hash"] if head else None, "anchored_day": anchor["day"] if anchor else None}


class PgDsr:
    def __init__(self, conn: AsyncConnection, tenant_id: str, priv: AsyncEngine | None):
        self.c, self.tenant, self.priv = conn, tenant_id, priv

    async def create(self, driver_id, kind) -> Row:
        r = await _one(self.c, q.DSR_CREATE, {"driver_id": driver_id, "kind": kind})
        assert r is not None
        return r

    async def get(self, dsr_id) -> Row | None:
        return await _one(self.c, q.DSR_GET, {"id": dsr_id})

    async def load_subject(self, driver_id) -> Row | None:
        return await _one(self.c, q.DSR_SUBJECT, {"id": driver_id})

    async def destroy_key(self, subject_key_id) -> bool:
        return (await self.c.execute(text(q.KEY_DESTROY), {"key": subject_key_id})).rowcount > 0

    async def read_key(self, subject_key_id) -> Row | None:
        return await _one(self.c, q.KEY_READ, {"key": subject_key_id})

    async def null_pii(self, driver_id) -> int:
        return (await self.c.execute(text(q.DRIVER_NULL_PII), {"id": driver_id})).rowcount

    async def pseudonymize_trips(self, driver_id) -> int:
        if self.priv is not None:  # fs_worker fallback (explicit tenant predicate, own transaction), see 0007_grants gap
            async with self.priv.begin() as pc:
                return (await pc.execute(text(q.TRIP_PSEUDONYMIZE_PRIV), {"tenant": self.tenant, "id": driver_id})).rowcount
        return (await self.c.execute(text(q.TRIP_PSEUDONYMIZE), {"id": driver_id})).rowcount

    async def subject_export(self, driver_id) -> Row:
        d = await _one(self.c, q.DSR_SUBJECT, {"id": driver_id}) or {}
        trips = await _all(self.c, q.EXPORT_TRIPS, {"id": driver_id})
        assigns = await _all(self.c, q.EXPORT_ASSIGNMENTS, {"id": driver_id})
        return {
            "driver": {"id": str(d.get("id")), "fleet_id": str(d.get("fleet_id")), "safety_score": float(d["safety_score"]) if d.get("safety_score") is not None else None,
                       "created_at": d.get("created_at"), "erased_at": d.get("erased_at")},
            "trips": [{"id": str(t["id"]), "start_ts": t["start_ts"], "end_ts": t["end_ts"], "distance_km": float(t["distance_km"])} for t in trips],
            "assignments": assigns,
        }

    async def finish(self, dsr_id, status, evidence) -> Row:
        r = await _one(self.c, q.DSR_FINISH, {"id": dsr_id, "status": status, "evidence": _j(evidence)})
        assert r is not None
        return r


class PgReports:
    def __init__(self, conn: AsyncConnection):
        self.c = conn

    async def cost_report(self, date_from, date_to, group_by) -> Row:
        p = {"date_from": date_from, "date_to": date_to}
        series = [{"key": r["key"], "energy_cost": float(r["energy_cost"]), "idle_cost": float(r["idle_cost"]), "distance_km": float(r["distance_km"])}
                  for r in await _all(self.c, q.COST_SQL[group_by], p)]
        sav = await _one(self.c, q.COST_SAVINGS, p)
        return {
            "total_cost": round(sum(s["energy_cost"] for s in series), 2), "idle_cost": round(sum(s["idle_cost"] for s in series), 2),
            "savings_opportunity": round(float(sav["savings"]) if sav else 0.0, 2), "series": series,
        }

    async def insight_rows(self, date_from, date_to) -> list[tuple[int, float, float, float, int]]:
        rows = await _all(self.c, q.INSIGHT_ROWS, {"date_from": date_from, "date_to": date_to})
        return [(int(r["vk"]), float(r["lat"]), float(r["lon"]), float(r["idle_s"]), int(r["harsh"])) for r in rows]

    async def tenant_vins(self) -> list[str]:
        return [str(r["vin"]).strip() for r in await _all(self.c, q.TENANT_VINS)]


__all__ = ["PgTenantDb", "make_engine", "datetime", "UTC"]
