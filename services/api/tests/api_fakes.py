"""In-memory fakes for every port. FakeTenantDb simulates Row-Level Security: every repo call is filtered by the tenant the
transaction was opened for, so BOLA/tenant-isolation unit tests exercise the same contract as the real RLS."""
from __future__ import annotations

import copy
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fs_api.ports import AgentUnavailable, Repos


def _uid() -> str:
    return str(uuid.uuid4())


def _dt(v: Any) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(v)


class Data:
    """Shared in-memory 'database'. Rows carry tenant_id exactly like the real tables."""

    def __init__(self) -> None:
        self.vehicles: list[dict] = []
        self.alerts: list[dict] = []
        self.trips: list[dict] = []
        self.drivers: list[dict] = []
        self.subject_keys: dict[str, dict] = {}
        self.risk: list[dict] = []
        self.signatures: list[dict] = []
        self.actions: list[dict] = []
        self.work_orders: list[dict] = []
        self.mappings: list[dict] = []
        self.audit: list[dict] = []
        self.dsr: list[dict] = []
        self.insight_rows: dict[str, list[tuple]] = {}
        self.cost: dict[str, dict] = {}
        self.users: dict[str, str] = {}
        self.oems = {"OEM_A", "OEM_B", "OEM_C", "OEM_D"}
        self.fail_pseudonymize = False
        self.tx_opened: list[str] = []

    # -- builders used by tests
    def add_vehicle(self, tenant: str, vin: str, fleet: str | None = None, risk: float = 0.1, oem: str = "OEM_A",
                    model: str = "Aurora Sprint", powertrain: str = "ICE") -> dict:
        v = {"id": _uid(), "tenant_id": tenant, "vin": vin, "fleet_id": fleet or _uid(), "oem": oem, "model": model,
             "powertrain": powertrain, "last_risk_score": risk}
        self.vehicles.append(v)
        return v

    def add_alert(self, tenant: str, vin: str, rule: str = "TOW", severity: str = "CRITICAL", status: str = "open",
                  opened_at: datetime | None = None) -> dict:
        v = next(x for x in self.vehicles if x["vin"] == vin and x["tenant_id"] == tenant)
        a = {"id": _uid(), "tenant_id": tenant, "vehicle_id": v["id"], "vin": vin, "rule_code": rule, "severity": severity,
             "status": status, "occurrences": 1, "summary": f"{rule} on {vin}", "details": {}, "impact_at_creation": 12.5,
             "opened_at": opened_at or datetime.now(UTC), "event_ts": None, "detected_ts": None, "ingest_ts": None,
             "trace_id": "t1", "lat": 12.9716, "lon": 77.5946}
        self.alerts.append(a)
        return a

    def add_trip(self, tenant: str, vin: str, start: datetime, driver_id: str | None = None, km: float = 10.0) -> dict:
        v = next(x for x in self.vehicles if x["vin"] == vin and x["tenant_id"] == tenant)
        t = {"id": _uid(), "tenant_id": tenant, "vehicle_id": v["id"], "start_ts": start, "end_ts": start + timedelta(minutes=30),
             "distance_km": km, "idle_s": 60, "energy_used": 1.0, "energy_cost": 1.5, "harsh_brake_count": 1,
             "harsh_accel_count": 0, "driver_id": driver_id}
        self.trips.append(t)
        return t

    def add_driver(self, tenant: str, key_id: str, name_enc: bytes | None, license_enc: bytes | None, wrapped_dek: bytes | None,
                   score: float | None = 80.0) -> dict:
        self.subject_keys[key_id] = {"subject_key_id": key_id, "tenant_id": tenant, "wrapped_dek": wrapped_dek,
                                     "destroyed_at": None if wrapped_dek else datetime.now(UTC)}
        d = {"id": _uid(), "tenant_id": tenant, "fleet_id": _uid(), "subject_key_id": key_id, "name_enc": name_enc,
             "license_enc": license_enc, "safety_score": score, "safety_confidence": "ok", "erased_at": None,
             "created_at": datetime.now(UTC)}
        self.drivers.append(d)
        return d


class _Base:
    def __init__(self, data: Data, tenant: str):
        self.d, self.t = data, tenant

    def own(self, rows: list[dict]) -> list[dict]:
        return [r for r in rows if r["tenant_id"] == self.t]


class FakeVehicles(_Base):
    def _open(self, v: dict) -> int:
        return sum(1 for a in self.d.alerts if a["vehicle_id"] == v["id"] and a["status"] in ("open", "ack"))

    async def list_page(self, *, fleet_id, min_risk, vin_prefix, sort, after, limit):
        rows = self.own(self.d.vehicles)
        if fleet_id:
            rows = [r for r in rows if r["fleet_id"] == fleet_id]
        if min_risk is not None:
            rows = [r for r in rows if r["last_risk_score"] >= min_risk]
        if vin_prefix:
            rows = [r for r in rows if r["vin"].startswith(vin_prefix)]
        if sort == "risk_desc":
            rows.sort(key=lambda r: (-r["last_risk_score"], r["vin"]))
            if after:
                rows = [r for r in rows if r["last_risk_score"] < after[0] or (r["last_risk_score"] == after[0] and r["vin"] > after[1])]
        else:
            rows.sort(key=lambda r: r["vin"])
            if after:
                rows = [r for r in rows if r["vin"] > after[0]]
        return [{**r, "open_alerts": self._open(r)} for r in rows[:limit]]

    async def get(self, vin):
        for r in self.own(self.d.vehicles):
            if r["vin"] == vin:
                return {**r, "open_alerts": self._open(r)}
        return None

    async def trips(self, vin, after, limit):
        v = await self.get(vin)
        if v is None:
            return None
        rows = sorted([t for t in self.own(self.d.trips) if t["vehicle_id"] == v["id"]], key=lambda t: (t["start_ts"], t["id"]), reverse=True)
        if after:
            key = (_dt(after[0]), after[1])
            rows = [t for t in rows if (t["start_ts"], t["id"]) < key]
        return rows[:limit]

    async def recent_trips(self, vin, n):
        return (await self.trips(vin, None, n)) or []

    async def recent_alerts(self, vin, n):
        rows = sorted([a for a in self.own(self.d.alerts) if a["vin"] == vin], key=lambda a: (a["opened_at"], a["id"]), reverse=True)
        return rows[:n]

    async def latest_risk(self, vin):
        v = await self.get(vin)
        rows = [r for r in self.own(self.d.risk) if v and r["vehicle_id"] == v["id"]]
        return max(rows, key=lambda r: r["predicted_at"]) if rows else None

    async def lookalikes(self, vin, k):
        if await self.get(vin) is None:
            return None
        return [s for s in self.own(self.d.signatures) if s["vin"] != vin][:k]

    async def fleet_summary(self, fleet_id, risk_threshold):
        vs = [v for v in self.own(self.d.vehicles) if not fleet_id or v["fleet_id"] == fleet_id]
        crit = [a for a in self.own(self.d.alerts) if a["severity"] == "CRITICAL" and a["status"] in ("open", "ack")]
        return {"vehicles_total": len(vs), "at_risk_vehicles": sum(1 for v in vs if v["last_risk_score"] >= risk_threshold),
                "open_critical_alerts": len(crit), "leaking_usd_today": 42.5, "savings_opportunity_usd_week": 310.0}


class FakeAlerts(_Base):
    async def list_page(self, *, statuses, severity, rule, vin, since, after, limit):
        rows = self.own(self.d.alerts)
        if statuses:
            rows = [a for a in rows if a["status"] in statuses]
        if severity:
            rows = [a for a in rows if a["severity"] == severity]
        if rule:
            rows = [a for a in rows if a["rule_code"] == rule]
        if vin:
            rows = [a for a in rows if a["vin"] == vin]
        if since:
            rows = [a for a in rows if a["opened_at"] >= _dt(since)]
        rows.sort(key=lambda a: (a["opened_at"], a["id"]), reverse=True)
        if after:
            key = (_dt(after[0]), after[1])
            rows = [a for a in rows if (a["opened_at"], a["id"]) < key]
        return rows[:limit]

    async def get(self, alert_id):
        return next((a for a in self.own(self.d.alerts) if a["id"] == alert_id), None)

    async def set_status(self, alert_id, status, reviewer_sub):
        a = await self.get(alert_id)
        if a is None:
            return None
        ok = a["status"] == "open" if status == "ack" else a["status"] in ("open", "ack")
        if not ok:
            return None
        a["status"] = status
        return a


class FakeDrivers(_Base):
    def _row(self, d: dict) -> dict:
        sk = self.d.subject_keys.get(d["subject_key_id"], {})
        sc = d["safety_score"]
        return {**d, "wrapped_dek": sk.get("wrapped_dek"), "sort_score": 100.0 if sc is None else sc, "km_30d": 250.0}

    async def list_page(self, *, sort, fleet_id, after, limit):
        rows = [self._row(d) for d in self.own(self.d.drivers) if not fleet_id or d["fleet_id"] == fleet_id]
        rev = sort == "score_desc"
        rows.sort(key=lambda r: (-r["sort_score"] if rev else r["sort_score"], r["id"]))
        if after:
            s, i = after
            rows = [r for r in rows if (r["sort_score"] < s if rev else r["sort_score"] > s) or (r["sort_score"] == s and r["id"] > i)]
        return rows[:limit]

    async def get(self, driver_id):
        d = next((x for x in self.own(self.d.drivers) if x["id"] == driver_id), None)
        if d is None:
            return None
        return {**self._row(d), "per_100km": {"harsh_brake": 3.1, "harsh_accel": 0.5, "overspeed_min": 2.0, "night_share": 0.1, "idle_share": 0.05},
                "fleet_p50": {"harsh_brake": 1.2, "harsh_accel": 0.6, "overspeed_min": 3.0, "night_share": 0.1, "idle_share": 0.08}}


class FakeActions(_Base):
    def _out(self, a: dict) -> dict:
        wo = next((w for w in self.d.work_orders if w["agent_action_id"] == a["id"]), None)
        return {**a, "work_order_id": wo["id"] if wo else None}

    async def list_page(self, *, status, after, limit):
        rows = sorted(self.own(self.d.actions), key=lambda a: (a["created_at"], a["id"]), reverse=True)
        if status:
            rows = [a for a in rows if a["status"] == status]
        if after:
            key = (_dt(after[0]), after[1])
            rows = [a for a in rows if (a["created_at"], a["id"]) < key]
        return [self._out(a) for a in rows[:limit]]

    async def create(self, *, proposal, rationale, expected_impact_usd, alert_id, idempotency_key):
        for a in self.own(self.d.actions):
            if a["idempotency_key"] == idempotency_key:
                return self._out(a)
        a = {"id": _uid(), "tenant_id": self.t, "status": "pending", "proposal": proposal, "rationale": rationale,
             "expected_impact_usd": expected_impact_usd, "created_at": datetime.now(UTC) + timedelta(microseconds=len(self.d.actions)),
             "decided_at": None, "idempotency_key": idempotency_key}
        self.d.actions.append(a)
        return self._out(a)

    async def get(self, action_id):
        a = next((x for x in self.own(self.d.actions) if x["id"] == action_id), None)
        return self._out(a) if a else None

    async def decide(self, action_id, status, reviewer_sub, reason):
        a = next((x for x in self.own(self.d.actions) if x["id"] == action_id), None)
        if a is None or a["status"] != "pending":
            return None
        a["status"], a["decided_at"] = status, datetime.now(UTC)
        if status == "approved":
            self.d.work_orders.append({"id": _uid(), "tenant_id": self.t, "agent_action_id": a["id"]})
        return self._out(a)


class FakeMappings(_Base):
    def _find(self, oem, version):
        return next((m for m in self.d.mappings if m["oem"] == oem and m["version"] == version), None)

    async def list_all(self):
        return [{k: v for k, v in m.items() if k != "document"} for m in self.d.mappings]

    async def next_version(self, oem):
        if oem not in self.d.oems:
            return None
        return 1 + max([m["version"] for m in self.d.mappings if m["oem"] == oem], default=0)

    async def create_draft(self, oem, version, document, created_by):
        if self._find(oem, version):
            return None
        m = {"oem": oem, "version": version, "status": "draft", "document": document, "created_by": created_by,
             "created_at": datetime.now(UTC), "activated_at": None}
        self.d.mappings.append(m)
        return m

    async def get(self, oem, version):
        return self._find(oem, version)

    async def set_status(self, oem, version, status):
        m = self._find(oem, version)
        m["status"] = status
        return m

    async def activate(self, oem, version):
        for m in self.d.mappings:
            if m["oem"] == oem and m["status"] == "active":
                m["status"] = "retired"
        m = self._find(oem, version)
        m["status"], m["activated_at"] = "active", datetime.now(UTC)
        return m

    async def active_versions(self):
        return {m["oem"]: m["version"] for m in self.d.mappings if m["status"] == "active"}


class FakeAudit(_Base):
    async def list_page(self, *, actor_id, action, decision, after, limit):
        rows = sorted(self.own(self.d.audit), key=lambda r: r["seq"], reverse=True)
        if actor_id:
            rows = [r for r in rows if r["actor_id"] == actor_id]
        if action:
            rows = [r for r in rows if r["action"] == action]
        if decision:
            rows = [r for r in rows if r["decision"] == decision]
        if after:
            rows = [r for r in rows if r["seq"] < after[0]]
        return rows[:limit]

    async def verify(self, from_seq, to_seq):
        bad = getattr(self.d, "tampered_seq", None)
        n = len(self.d.audit)
        return {"ok": bad is None, "checked": n, "first_bad_seq": bad, "head_hash": "ab" * 32, "anchored_day": None}


class FakeDsr(_Base):
    async def create(self, driver_id, kind):
        r = {"id": _uid(), "tenant_id": self.t, "driver_id": driver_id, "kind": kind, "status": "received",
             "requested_at": datetime.now(UTC), "completed_at": None, "evidence": {}}
        self.d.dsr.append(r)
        return copy.copy(r)

    async def get(self, dsr_id):
        r = next((x for x in self.own(self.d.dsr) if x["id"] == dsr_id), None)
        return copy.deepcopy(r) if r else None

    async def load_subject(self, driver_id):
        d = next((x for x in self.own(self.d.drivers) if x["id"] == driver_id), None)
        if d is None:
            return None
        sk = self.d.subject_keys.get(d["subject_key_id"], {})
        return {**d, "wrapped_dek": sk.get("wrapped_dek"), "destroyed_at": sk.get("destroyed_at")}

    async def destroy_key(self, subject_key_id):
        sk = self.d.subject_keys.get(subject_key_id)
        if not sk or sk["tenant_id"] != self.t or sk["wrapped_dek"] is None:
            return False
        sk["wrapped_dek"], sk["destroyed_at"] = None, datetime.now(UTC)
        return True

    async def read_key(self, subject_key_id):
        sk = self.d.subject_keys.get(subject_key_id)
        return dict(sk) if sk and sk["tenant_id"] == self.t else None

    async def null_pii(self, driver_id):
        for d in self.own(self.d.drivers):
            if d["id"] == driver_id:
                d["name_enc"] = d["license_enc"] = None
                d["erased_at"] = d["erased_at"] or datetime.now(UTC)
                return 1
        return 0

    async def pseudonymize_trips(self, driver_id):
        if self.d.fail_pseudonymize:
            raise PermissionError("permission denied for table trip")
        n = 0
        for t in self.own(self.d.trips):
            if t["driver_id"] == driver_id:
                t["driver_id"] = None
                n += 1
        return n

    async def subject_export(self, driver_id):
        d = next(x for x in self.own(self.d.drivers) if x["id"] == driver_id)
        trips = [t for t in self.own(self.d.trips) if t["driver_id"] == driver_id]
        return {"driver": {"id": d["id"], "fleet_id": d["fleet_id"], "safety_score": d["safety_score"]},
                "trips": [{"id": t["id"], "start_ts": t["start_ts"].isoformat(), "distance_km": t["distance_km"]} for t in trips],
                "assignments": []}

    async def finish(self, dsr_id, status, evidence):
        r = next(x for x in self.own(self.d.dsr) if x["id"] == dsr_id)
        r["status"], r["evidence"] = status, evidence
        r["completed_at"] = datetime.now(UTC) if status == "completed" else None
        return copy.deepcopy(r)


class FakeReports(_Base):
    async def cost_report(self, date_from, date_to, group_by):
        return self.d.cost.get(self.t) or {"total_cost": 100.0, "idle_cost": 20.0, "savings_opportunity": 5.0,
                                           "series": [{"key": date_from, "energy_cost": 100.0, "idle_cost": 20.0, "distance_km": 50.0}]}

    async def insight_rows(self, date_from, date_to):
        return list(self.d.insight_rows.get(self.t, []))

    async def tenant_vins(self):
        return [v["vin"] for v in self.own(self.d.vehicles)]


class FakeTenantDb:
    def __init__(self, data: Data):
        self.data = data
        self.fail_next: Exception | None = None
        self.pings = 0

    @asynccontextmanager
    async def tx(self, tenant_id: str):
        if self.fail_next is not None:
            e, self.fail_next = self.fail_next, None
            raise e
        self.data.tx_opened.append(tenant_id)
        d, t = self.data, tenant_id
        yield Repos(FakeVehicles(d, t), FakeAlerts(d, t), FakeDrivers(d, t), FakeActions(d, t), FakeMappings(d, t),
                    FakeAudit(d, t), FakeDsr(d, t), FakeReports(d, t))

    async def ping(self):
        self.pings += 1


class FakeState:
    def __init__(self) -> None:
        self.states: dict[str, dict] = {}  # vin -> {tenant, lat, lon, status, ts_ms}
        self.flagged: dict[str, set[str]] = {}
        self.counts: dict[str, dict[str, int]] = {}
        self.freshness = 1240
        self.fallback = False
        self.purged: list[tuple[str, str]] = []
        self.down = False

    def put(self, tenant: str, vin: str, lat: float, lon: float, status: str = "DRIVING", ts_ms: int = 1_700_000_000_000) -> None:
        self.states[vin] = {"tenant": tenant, "vin": vin, "lat": lat, "lon": lon, "status": status, "ts_ms": ts_ms}

    def _chk(self):
        if self.down:
            raise ConnectionError("redis down")

    async def get_states(self, tenant_id, vins):
        self._chk()
        return {v: self.states[v] for v in vins if v in self.states and self.states[v]["tenant"] == tenant_id}

    async def viewport(self, tenant_id, west, south, east, north, limit):
        self._chk()
        rows = [s for s in self.states.values() if s["tenant"] == tenant_id and west <= s["lon"] <= east and south <= s["lat"] <= north]
        return rows[:limit], len(rows) > limit

    async def status_counts(self, tenant_id):
        self._chk()
        return self.counts.get(tenant_id, {})

    async def freshness_p95_ms(self):
        self._chk()
        return self.freshness

    async def model_fallback_active(self):
        self._chk()
        return self.fallback

    async def is_flagged(self, tenant_id, vin):
        return vin in self.flagged.get(tenant_id, set())

    async def flagged_set(self, tenant_id):
        return set(self.flagged.get(tenant_id, set()))

    async def purge_driver(self, tenant_id, driver_id):
        self.purged.append((tenant_id, driver_id))
        return 2

    async def ping(self):
        self._chk()


class FakeTwins:
    def __init__(self) -> None:
        self.docs: dict[tuple[str, str], dict] = {}
        self.down = False
        self.purged: list[tuple[str, str]] = []

    async def get(self, tenant_id, vin):
        if self.down:
            raise ConnectionError("mongo down")
        return copy.deepcopy(self.docs.get((tenant_id, vin)))

    async def purge_driver(self, tenant_id, driver_id):
        self.purged.append((tenant_id, driver_id))
        return 1

    async def ping(self):
        return None


class FakeAlertFeed:
    def __init__(self) -> None:
        self.queue: list[dict] = []

    @asynccontextmanager
    async def subscribe(self, tenant_id):
        outer = self

        class Feed:
            async def get(self, timeout: float = 0.0):
                return outer.queue.pop(0) if outer.queue else None

        yield Feed()


class FakeMappingPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []
        self.fail = False

    async def publish(self, oem, document):
        if self.fail:
            raise ConnectionError("kafka down")
        self.published.append((oem, document))


class FakeAgent:
    def __init__(self) -> None:
        self.down = False
        self.calls: list[tuple] = []

    async def chat(self, bearer, tenant_id, message, conversation_id):
        if self.down:
            raise AgentUnavailable("down")
        self.calls.append(("chat", tenant_id, message))
        return {"answer": f"echo: {message}", "grounded": True, "conversation_id": conversation_id or _uid(), "degraded": False}

    async def briefing(self, bearer, tenant_id, generate):
        if self.down:
            raise AgentUnavailable("down")
        self.calls.append(("briefing", tenant_id, generate))
        return {"generated_at": datetime.now(UTC), "source": "rules", "headline": "2 actions could save $900", "total_impact_usd": 900.0, "items": []}


class FakeReportRunner:
    async def run(self, report_id, params, vins):
        return {"report_id": report_id, "rows_scanned": len(vins) * 100, "elapsed_ms": 3, "columns": ["a", "b"], "rows": [[1, 2]]}


class FakeQuality:
    def __init__(self) -> None:
        self.replays: list[tuple] = []

    async def oem_quality(self):
        return [{"oem": "OEM_A", "records_1h": 1000, "failure_rate_1h": 0.01, "drift_suspected": False, "quality_score": 99.0},
                {"oem": "OEM_D", "records_1h": 500, "failure_rate_1h": 0.9, "drift_suspected": True, "drift_detail": "columnar", "quality_score": 12.0}]

    async def dlq_sample(self, oem, reason, limit):
        return [{"oem": oem or "OEM_D", "error_reason": reason or "MAPPING_MISSING", "ts": datetime.now(UTC), "payload": {"x": 1}}][:limit]

    async def replay_dlq(self, oem, reason, limit):
        self.replays.append((oem, reason, limit))
        return {"job_id": _uid(), "records": 7}


class MemoryIdem:
    def __init__(self) -> None:
        self.d: dict[str, dict] = {}
        self.begin_calls = 0

    async def begin(self, scope, fingerprint, ttl_s):
        self.begin_calls += 1
        cur = self.d.get(scope)
        if cur is None:
            self.d[scope] = {"state": "in_flight", "fp": fingerprint}
            return "new", None
        if cur["fp"] != fingerprint:
            return "mismatch", None
        return ("replay", cur["record"]) if cur["state"] == "done" else ("in_flight", None)

    async def complete(self, scope, record, ttl_s):
        self.d[scope] = {"state": "done", "fp": self.d[scope]["fp"], "record": record}

    async def abort(self, scope):
        self.d.pop(scope, None)
