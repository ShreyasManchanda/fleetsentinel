"""Hand-written SQL for the Postgres adapter (no ORM, no lazy loading). Every tenant-scoped statement filters on
app_tenant() explicitly (planner-friendly tenant-leading indexes) IN ADDITION to Row-Level Security (defence in depth).

Dynamic statements are assembled ONLY from fixed fragments below; user input is always a bound parameter."""
from __future__ import annotations

from typing import Any

VEHICLE_SELECT = """
SELECT v.vin, v.fleet_id, o.code AS oem, m.name AS model, m.powertrain, v.last_risk_score,
       COALESCE(a.n, 0) AS open_alerts
FROM vehicle v
JOIN vehicle_model m ON m.id = v.model_id
JOIN oem o ON o.id = m.oem_id
LEFT JOIN LATERAL (
    SELECT count(*) AS n FROM alert al
    WHERE al.vehicle_id = v.id AND al.tenant_id = v.tenant_id AND al.status IN ('open', 'ack')
) a ON true
WHERE v.tenant_id = app_tenant()
"""


def vehicle_list_sql(fleet: bool, min_risk: bool, prefix: bool, after: bool, sort: str) -> str:
    sql = VEHICLE_SELECT
    if fleet:
        sql += " AND v.fleet_id = CAST(:fleet_id AS uuid)"
    if min_risk:
        sql += " AND v.last_risk_score >= :min_risk"
    if prefix:
        sql += " AND v.vin LIKE :prefix"
    if sort == "risk_desc":
        if after:
            sql += " AND (v.last_risk_score < :k_risk OR (v.last_risk_score = :k_risk AND v.vin > :k_vin))"
        sql += " ORDER BY v.last_risk_score DESC, v.vin ASC"
    else:
        if after:
            sql += " AND v.vin > :k_vin"
        sql += " ORDER BY v.vin ASC"
    return sql + " LIMIT :limit"


VEHICLE_GET = """
SELECT v.id, v.vin, v.fleet_id, o.code AS oem, m.name AS model, m.powertrain, v.last_risk_score,
       (SELECT count(*) FROM alert al WHERE al.vehicle_id = v.id AND al.tenant_id = v.tenant_id AND al.status IN ('open', 'ack')) AS open_alerts
FROM vehicle v
JOIN vehicle_model m ON m.id = v.model_id
JOIN oem o ON o.id = m.oem_id
WHERE v.vin = :vin AND v.tenant_id = app_tenant()
"""

VEHICLE_ID = "SELECT id FROM vehicle WHERE vin = :vin AND tenant_id = app_tenant()"

TRIP_COLS = """
SELECT t.id, t.start_ts, t.end_ts, t.distance_km, t.idle_s, t.energy_used, t.energy_cost,
       t.harsh_brake_count, t.harsh_accel_count, t.driver_id
FROM trip t
WHERE t.tenant_id = app_tenant() AND t.vehicle_id = (SELECT id FROM vehicle WHERE vin = :vin AND tenant_id = app_tenant())
"""


def trips_sql(after: bool) -> str:
    sql = TRIP_COLS
    if after:
        sql += " AND (t.start_ts, t.id) < (CAST(:k_ts AS timestamptz), CAST(:k_id AS uuid))"
    return sql + " ORDER BY t.start_ts DESC, t.id DESC LIMIT :limit"


ALERT_COLS = """
SELECT a.id, v.vin, a.rule_code, a.severity, a.status, a.occurrences, a.summary, a.details, a.impact_at_creation,
       a.opened_at, a.event_ts, a.detected_ts, a.ingest_ts, a.trace_id, a.lat, a.lon
FROM alert a
JOIN vehicle v ON v.id = a.vehicle_id AND v.tenant_id = a.tenant_id
WHERE a.tenant_id = app_tenant()
"""

VEHICLE_RECENT_ALERTS = ALERT_COLS + """
  AND a.vehicle_id = (SELECT id FROM vehicle WHERE vin = :vin AND tenant_id = app_tenant())
ORDER BY a.opened_at DESC, a.id DESC LIMIT :n
"""

LATEST_RISK = """
SELECT rp.p_failure_7d, rp.reason_codes, rp.predicted_at, mv.name AS model_name, mv.version AS model_version
FROM risk_prediction rp
JOIN model_version mv ON mv.id = rp.model_version_id
WHERE rp.tenant_id = app_tenant()
  AND rp.vehicle_id = (SELECT id FROM vehicle WHERE vin = :vin AND tenant_id = app_tenant())
ORDER BY rp.predicted_at DESC LIMIT 1
"""

# pgvector kNN under RLS: nearest pre-failure signatures to the vehicle latest signature (cosine distance, HNSW index)
LOOKALIKES = """
WITH q AS (
    SELECT s.embedding FROM vehicle_signature s
    WHERE s.tenant_id = app_tenant() AND s.vehicle_id = (SELECT id FROM vehicle WHERE vin = :vin AND tenant_id = app_tenant())
    ORDER BY s.day DESC LIMIT 1
)
SELECT v.vin, 1 - (s.embedding <=> q.embedding) AS similarity, s.failed_7d
FROM q
CROSS JOIN LATERAL (
    SELECT s2.vehicle_id, s2.embedding, s2.failed_7d FROM vehicle_signature s2
    WHERE s2.tenant_id = app_tenant() AND s2.failed_7d IS TRUE
    ORDER BY s2.embedding <=> q.embedding LIMIT :k_over
) s
JOIN vehicle v ON v.id = s.vehicle_id AND v.tenant_id = app_tenant()
WHERE v.vin <> :vin
ORDER BY similarity DESC LIMIT :k
"""

FLEET_SUMMARY = """
WITH veh AS (
    SELECT count(*) AS vehicles_total, count(*) FILTER (WHERE last_risk_score >= :thr) AS at_risk
    FROM vehicle
    WHERE tenant_id = app_tenant() AND (CAST(:fleet_id AS uuid) IS NULL OR fleet_id = CAST(:fleet_id AS uuid))
),
crit AS (
    SELECT count(*) AS n FROM alert a
    WHERE a.tenant_id = app_tenant() AND a.status IN ('open', 'ack') AND a.severity = 'CRITICAL'
      AND (CAST(:fleet_id AS uuid) IS NULL OR a.vehicle_id IN (SELECT id FROM vehicle WHERE fleet_id = CAST(:fleet_id AS uuid)))
),
leak AS (
    SELECT COALESCE(sum(idle_cost), 0) AS today FROM v_telemetry_1m
    WHERE bucket >= date_trunc('day', now()) AND (CAST(:fleet_id AS uuid) IS NULL OR fleet_id = CAST(:fleet_id AS uuid))
),
pv AS (
    SELECT vehicle_id, sum(idle_cost) AS c FROM v_telemetry_1m
    WHERE bucket >= now() - interval '7 days' AND (CAST(:fleet_id AS uuid) IS NULL OR fleet_id = CAST(:fleet_id AS uuid))
    GROUP BY vehicle_id
),
p25 AS (SELECT percentile_cont(0.25) WITHIN GROUP (ORDER BY c) AS p FROM pv)
SELECT veh.vehicles_total, veh.at_risk AS at_risk_vehicles, crit.n AS open_critical_alerts, leak.today AS leaking_usd_today,
       COALESCE((SELECT sum(GREATEST(pv.c - p25.p, 0)) FROM pv, p25), 0) AS savings_opportunity_usd_week
FROM veh, crit, leak
"""


def alert_list_sql(statuses: list[str] | None, severity: bool, rule: bool, vin: bool, since: bool, after: bool) -> str:
    sql = ALERT_COLS
    if statuses:
        allowed = {"open", "ack", "resolved"}
        lits = ", ".join(f"'{s}'" for s in statuses if s in allowed)  # whitelist-validated literals keep the partial index usable
        sql += f" AND a.status IN ({lits})"
    if severity:
        sql += " AND a.severity = :severity"
    if rule:
        sql += " AND a.rule_code = :rule"
    if vin:
        sql += " AND v.vin = :vin"
    if since:
        sql += " AND a.opened_at >= CAST(:since AS timestamptz)"
    if after:
        sql += " AND (a.opened_at, a.id) < (CAST(:k_ts AS timestamptz), CAST(:k_id AS uuid))"
    return sql + " ORDER BY a.opened_at DESC, a.id DESC LIMIT :limit"


ALERT_GET = ALERT_COLS + " AND a.id = CAST(:id AS uuid)"

ALERT_ACK = """
UPDATE alert SET status = 'ack',
       acknowledged_by = (SELECT id FROM user_account WHERE keycloak_sub = :sub AND tenant_id = app_tenant())
WHERE id = CAST(:id AS uuid) AND tenant_id = app_tenant() AND status = 'open' RETURNING id
"""
ALERT_RESOLVE = """
UPDATE alert SET status = 'resolved', resolved_at = now()
WHERE id = CAST(:id AS uuid) AND tenant_id = app_tenant() AND status IN ('open', 'ack') RETURNING id
"""

DRIVER_COLS = """
SELECT d.id, d.name_enc, d.subject_key_id, sk.wrapped_dek, d.safety_score, d.safety_confidence, d.erased_at,
       COALESCE(d.safety_score, 100) AS sort_score
FROM driver d
LEFT JOIN subject_key sk ON sk.subject_key_id = d.subject_key_id AND sk.tenant_id = d.tenant_id
WHERE d.tenant_id = app_tenant()
"""


def driver_list_sql(fleet: bool, after: bool, sort: str) -> str:
    sql = DRIVER_COLS
    if fleet:
        sql += " AND d.fleet_id = CAST(:fleet_id AS uuid)"
    if after:
        op = ">" if sort == "score_asc" else "<"
        sql += (f" AND (COALESCE(d.safety_score, 100) {op} :k_score OR "
                f"(COALESCE(d.safety_score, 100) = :k_score AND d.id > CAST(:k_id AS uuid)))")
    order = "ASC" if sort == "score_asc" else "DESC"
    return sql + f" ORDER BY COALESCE(d.safety_score, 100) {order}, d.id ASC LIMIT :limit"


DRIVER_DETAIL = """
WITH me AS (
    SELECT d.id, d.name_enc, d.subject_key_id, sk.wrapped_dek, d.safety_score, d.safety_confidence, d.erased_at
    FROM driver d
    LEFT JOIN subject_key sk ON sk.subject_key_id = d.subject_key_id AND sk.tenant_id = d.tenant_id
    WHERE d.id = CAST(:id AS uuid) AND d.tenant_id = app_tenant()
),
tt AS (
    SELECT t.driver_id, sum(t.distance_km) AS km, sum(t.harsh_brake_count) AS hb, sum(t.harsh_accel_count) AS ha,
           sum(t.overspeed_s) AS os, sum(t.night_share * t.distance_km) AS night_km, sum(t.idle_s) AS idle_s,
           sum(extract(epoch FROM (t.end_ts - t.start_ts))) AS dur_s
    FROM trip t
    WHERE t.tenant_id = app_tenant() AND t.driver_id IS NOT NULL AND t.start_ts >= now() - interval '30 days'
    GROUP BY t.driver_id HAVING sum(t.distance_km) > 0
),
per AS (
    SELECT driver_id, km, hb / km * 100 AS harsh_brake, ha / km * 100 AS harsh_accel, os / 60.0 / km * 100 AS overspeed_min,
           night_km / km AS night_share, idle_s / NULLIF(dur_s, 0) AS idle_share
    FROM tt
),
p50 AS (
    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY harsh_brake) AS p_harsh_brake,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY harsh_accel) AS p_harsh_accel,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY overspeed_min) AS p_overspeed_min,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY night_share) AS p_night_share,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY idle_share) AS p_idle_share
    FROM per
)
SELECT me.*, per.km AS km_30d, per.harsh_brake, per.harsh_accel, per.overspeed_min, per.night_share, per.idle_share,
       p50.p_harsh_brake, p50.p_harsh_accel, p50.p_overspeed_min, p50.p_night_share, p50.p_idle_share
FROM me LEFT JOIN per ON per.driver_id = me.id CROSS JOIN p50
"""

ACTION_COLS = """
SELECT a.id, a.status, a.proposal, a.rationale, a.expected_impact_usd, a.created_at, a.decided_at, w.id AS work_order_id
FROM agent_action a
LEFT JOIN work_order w ON w.agent_action_id = a.id AND w.tenant_id = a.tenant_id
WHERE a.tenant_id = app_tenant()
"""


def action_list_sql(status: bool, after: bool) -> str:
    sql = ACTION_COLS
    if status:
        sql += " AND a.status = :status"
    if after:
        sql += " AND (a.created_at, a.id) < (CAST(:k_ts AS timestamptz), CAST(:k_id AS uuid))"
    return sql + " ORDER BY a.created_at DESC, a.id DESC LIMIT :limit"


ACTION_GET = ACTION_COLS + " AND a.id = CAST(:id AS uuid)"
ALERT_EXISTS = "SELECT 1 FROM alert WHERE id = CAST(:id AS uuid) AND tenant_id = app_tenant()"
ACTION_CREATE = """
INSERT INTO agent_action (tenant_id, alert_id, status, proposal, rationale, expected_impact_usd, idempotency_key)
VALUES (app_tenant(), CAST(:alert_id AS uuid), 'pending', CAST(:proposal AS jsonb), :rationale, :impact, :key)
ON CONFLICT (tenant_id, idempotency_key) DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key
RETURNING id
"""
ACTION_DECIDE = """
UPDATE agent_action SET status = :status, decided_at = now(), decided_reason = :reason,
       reviewer_id = (SELECT id FROM user_account WHERE keycloak_sub = :sub AND tenant_id = app_tenant())
WHERE id = CAST(:id AS uuid) AND tenant_id = app_tenant() AND status = 'pending' RETURNING id
"""
WORK_ORDER_CREATE = """
INSERT INTO work_order (tenant_id, agent_action_id) VALUES (app_tenant(), CAST(:id AS uuid))
ON CONFLICT (agent_action_id) DO NOTHING
"""

MAPPING_LIST = """
SELECT o.code AS oem, s.version, s.status, s.created_by, s.created_at, s.activated_at
FROM schema_mapping s JOIN oem o ON o.id = s.oem_id ORDER BY o.code, s.version DESC
"""
MAPPING_NEXT_VERSION = """
SELECT COALESCE(max(s.version), 0) + 1 AS next_version
FROM oem o LEFT JOIN schema_mapping s ON s.oem_id = o.id WHERE o.code = :oem GROUP BY o.id
"""
MAPPING_CREATE = """
INSERT INTO schema_mapping (oem_id, version, status, document, created_by)
SELECT o.id, :version, 'draft', CAST(:document AS jsonb), :created_by FROM oem o WHERE o.code = :oem
ON CONFLICT (oem_id, version) DO NOTHING
RETURNING version
"""
MAPPING_GET = """
SELECT o.code AS oem, s.version, s.status, s.document, s.created_by, s.created_at, s.activated_at
FROM schema_mapping s JOIN oem o ON o.id = s.oem_id WHERE o.code = :oem AND s.version = :version
"""
MAPPING_APPROVE = """
UPDATE schema_mapping SET status = 'approved'
WHERE version = :version AND status = 'draft' AND oem_id = (SELECT id FROM oem WHERE code = :oem)
"""
MAPPING_RETIRE_ACTIVE = """
UPDATE schema_mapping SET status = 'retired' WHERE status = 'active' AND oem_id = (SELECT id FROM oem WHERE code = :oem)
"""
MAPPING_ACTIVATE = """
UPDATE schema_mapping SET status = 'active', activated_at = now()
WHERE version = :version AND status = 'approved' AND oem_id = (SELECT id FROM oem WHERE code = :oem)
"""
MAPPING_ACTIVE_VERSIONS = """
SELECT o.code AS oem, s.version FROM schema_mapping s JOIN oem o ON o.id = s.oem_id WHERE s.status = 'active'
"""


def audit_list_sql(actor: bool, action: bool, decision: bool, after: bool) -> str:
    sql = ("SELECT seq, ts, actor_id, actor_type, action, resource_type, resource_ids, purpose, decision, reason, hash "
           "FROM audit_log WHERE tenant_id = app_tenant()")
    if actor:
        sql += " AND actor_id = :actor_id"
    if action:
        sql += " AND action = :action"
    if decision:
        sql += " AND decision = :decision"
    if after:
        sql += " AND seq < :k_seq"
    return sql + " ORDER BY seq DESC LIMIT :limit"


AUDIT_VERIFY = "SELECT ok, checked, first_bad_seq FROM audit_verify(:from_seq, :to_seq)"
AUDIT_HEAD = ("SELECT encode(hash, 'hex') AS head_hash FROM audit_log "
              "WHERE tenant_id = app_tenant() AND seq BETWEEN :from_seq AND :to_seq ORDER BY seq DESC LIMIT 1")
AUDIT_ANCHOR = "SELECT day FROM audit_anchor ORDER BY day DESC LIMIT 1"

DSR_COLS = "id, kind, status, requested_at, completed_at, evidence"
DSR_CREATE = (f"INSERT INTO dsr_request (tenant_id, driver_id, kind, status) VALUES (app_tenant(), CAST(:driver_id AS uuid), :kind, 'received') "
              f"RETURNING {DSR_COLS}")
DSR_GET = f"SELECT {DSR_COLS} FROM dsr_request WHERE id = CAST(:id AS uuid) AND tenant_id = app_tenant()"
DSR_FINISH = (f"UPDATE dsr_request SET status = :status, completed_at = CASE WHEN :status = 'completed' THEN now() ELSE NULL END, "
              f"evidence = CAST(:evidence AS jsonb) WHERE id = CAST(:id AS uuid) AND tenant_id = app_tenant() RETURNING {DSR_COLS}")
DSR_SUBJECT = """
SELECT d.id, d.fleet_id, d.subject_key_id, d.name_enc, d.license_enc, d.erased_at, d.safety_score, d.created_at,
       sk.wrapped_dek, sk.destroyed_at
FROM driver d LEFT JOIN subject_key sk ON sk.subject_key_id = d.subject_key_id AND sk.tenant_id = d.tenant_id
WHERE d.id = CAST(:id AS uuid) AND d.tenant_id = app_tenant()
"""
KEY_DESTROY = ("UPDATE subject_key SET wrapped_dek = NULL, destroyed_at = now() "
               "WHERE subject_key_id = :key AND tenant_id = app_tenant() AND wrapped_dek IS NOT NULL")
KEY_READ = "SELECT subject_key_id, wrapped_dek, destroyed_at FROM subject_key WHERE subject_key_id = :key AND tenant_id = app_tenant()"
DRIVER_NULL_PII = ("UPDATE driver SET name_enc = NULL, license_enc = NULL, erased_at = COALESCE(erased_at, now()) "
                   "WHERE id = CAST(:id AS uuid) AND tenant_id = app_tenant()")
# Needs UPDATE(driver_id) on trip for fs_app (missing from 0007_grants, see BUGS) or the privileged fs_worker engine.
TRIP_PSEUDONYMIZE = "UPDATE trip SET driver_id = NULL WHERE tenant_id = app_tenant() AND driver_id = CAST(:id AS uuid)"
TRIP_PSEUDONYMIZE_PRIV = "UPDATE trip SET driver_id = NULL WHERE tenant_id = CAST(:tenant AS uuid) AND driver_id = CAST(:id AS uuid)"
EXPORT_TRIPS = ("SELECT id, start_ts, end_ts, distance_km FROM trip WHERE tenant_id = app_tenant() AND driver_id = CAST(:id AS uuid) "
                "ORDER BY start_ts DESC LIMIT 5000")
EXPORT_ASSIGNMENTS = ("SELECT v.vin, lower(da.valid_during) AS from_ts, upper(da.valid_during) AS to_ts FROM driver_assignment da "
                      "JOIN vehicle v ON v.id = da.vehicle_id AND v.tenant_id = da.tenant_id "
                      "WHERE da.tenant_id = app_tenant() AND da.driver_id = CAST(:id AS uuid) ORDER BY 2 DESC LIMIT 1000")

COST_WINDOW = "CAST(:date_from AS date)"
COST_END = "(CAST(:date_to AS date) + 1)"
COST_BY_DAY = f"""
WITH c AS (
    SELECT day::date AS k, sum(energy_cost) AS energy_cost, sum(distance_km) AS distance_km FROM v_fleet_cost_daily
    WHERE day >= {COST_WINDOW} AND day < {COST_END} GROUP BY 1
), i AS (
    SELECT date_trunc('day', hour)::date AS k, sum(idle_cost) AS idle_cost FROM v_fleet_metrics_1h
    WHERE hour >= {COST_WINDOW} AND hour < {COST_END} GROUP BY 1
)
SELECT k::text AS key, COALESCE(c.energy_cost, 0) AS energy_cost, COALESCE(i.idle_cost, 0) AS idle_cost, COALESCE(c.distance_km, 0) AS distance_km
FROM c FULL JOIN i USING (k) ORDER BY 1
"""
COST_BY_FLEET = f"""
WITH c AS (
    SELECT fleet_id AS k, sum(energy_cost) AS energy_cost, sum(distance_km) AS distance_km FROM v_fleet_cost_daily
    WHERE day >= {COST_WINDOW} AND day < {COST_END} GROUP BY 1
), i AS (
    SELECT fleet_id AS k, sum(idle_cost) AS idle_cost FROM v_fleet_metrics_1h
    WHERE hour >= {COST_WINDOW} AND hour < {COST_END} GROUP BY 1
), j AS (
    SELECT COALESCE(c.k, i.k) AS k, c.energy_cost, i.idle_cost, c.distance_km FROM c FULL JOIN i ON i.k = c.k
)
SELECT COALESCE(f.name, j.k::text) AS key, COALESCE(j.energy_cost, 0) AS energy_cost, COALESCE(j.idle_cost, 0) AS idle_cost,
       COALESCE(j.distance_km, 0) AS distance_km
FROM j LEFT JOIN fleet f ON f.id = j.k AND f.tenant_id = app_tenant() ORDER BY 1
"""
COST_BY_POWERTRAIN = f"""
WITH c AS (
    SELECT m.powertrain AS k, sum(t.energy_cost) AS energy_cost, sum(t.distance_km) AS distance_km
    FROM trip t JOIN vehicle v ON v.id = t.vehicle_id JOIN vehicle_model m ON m.id = v.model_id
    WHERE t.tenant_id = app_tenant() AND t.start_ts >= {COST_WINDOW} AND t.start_ts < {COST_END} GROUP BY 1
), i AS (
    SELECT m.powertrain AS k, sum(tm.idle_cost) AS idle_cost
    FROM v_telemetry_1m tm JOIN vehicle v ON v.id = tm.vehicle_id JOIN vehicle_model m ON m.id = v.model_id
    WHERE tm.bucket >= {COST_WINDOW} AND tm.bucket < {COST_END} GROUP BY 1
)
SELECT k AS key, COALESCE(c.energy_cost, 0) AS energy_cost, COALESCE(i.idle_cost, 0) AS idle_cost, COALESCE(c.distance_km, 0) AS distance_km
FROM c FULL JOIN i USING (k) ORDER BY 1
"""
COST_SAVINGS = f"""
WITH pv AS (
    SELECT vehicle_id, sum(idle_cost) AS c FROM v_telemetry_1m WHERE bucket >= {COST_WINDOW} AND bucket < {COST_END} GROUP BY 1
), p AS (SELECT percentile_cont(0.25) WITHIN GROUP (ORDER BY c) AS p25 FROM pv)
SELECT COALESCE(sum(GREATEST(pv.c - p.p25, 0)), 0) AS savings FROM pv, p
"""
COST_SQL = {"day": COST_BY_DAY, "fleet": COST_BY_FLEET, "powertrain": COST_BY_POWERTRAIN}

# Rows are (vehicle, ~110 m cell) so the API can generalise to geohash-5 and count DISTINCT vehicles per cell (k-anonymity).
INSIGHT_ROWS = f"""
SELECT hashtextextended(vehicle_id::text, 0) AS vk, round(last_lat::numeric, 3)::float8 AS lat, round(last_lon::numeric, 3)::float8 AS lon,
       sum(idle_s)::float8 AS idle_s, sum(harsh_brake + harsh_accel)::bigint AS harsh
FROM v_telemetry_1m
WHERE bucket >= {COST_WINDOW} AND bucket < {COST_END} AND last_lat IS NOT NULL AND last_lon IS NOT NULL
GROUP BY vehicle_id, 2, 3 LIMIT 500000
"""
TENANT_VINS = "SELECT vin FROM vehicle WHERE tenant_id = app_tenant() ORDER BY vin"

STARTS = ("SELECT", "WITH", "UPDATE", "INSERT")
ALL_STATIC: dict[str, Any] = {
    k: v for k, v in list(globals().items()) if isinstance(v, str) and k.isupper() and v.lstrip().upper().startswith(STARTS)
}
