-- 0006_perf: query optimisations. Deliberately a SEPARATE migration so the SQL write-up can capture EXPLAIN ANALYZE
-- "before" plans at `fs migrate --to 5`, then apply this and capture the "after" plans (see docs/sql-writeup.md).
-- Statements are separated by "-- @@".

-- Case 1: open-alert queue per tenant -> partial composite index (also matches the RLS predicate: tenant_id leads).
CREATE INDEX alert_open_queue ON alert (tenant_id, status, opened_at DESC) WHERE status IN ('open', 'ack');
-- @@
CREATE INDEX alert_tenant_opened ON alert (tenant_id, opened_at DESC, id DESC);
-- @@
-- Case 3: keyset pagination on trips. Cursor = (start_ts, id).
CREATE INDEX trip_keyset ON trip (tenant_id, vehicle_id, start_ts DESC, id DESC);
-- @@
CREATE INDEX trip_tenant_keyset ON trip (tenant_id, start_ts DESC, id DESC);
-- @@
CREATE INDEX risk_prediction_tenant_time ON risk_prediction (tenant_id, predicted_at DESC);
-- @@
-- Case 2: fleet cost summary -> materialised view, refreshed on a schedule (REFRESH ... CONCURRENTLY needs the unique index).
CREATE MATERIALIZED VIEW mv_fleet_cost_daily AS
SELECT t.tenant_id, v.fleet_id, date_trunc('day', t.start_ts) AS day,
       count(*)                AS trips,
       sum(t.distance_km)      AS distance_km,
       sum(t.idle_s)           AS idle_s,
       sum(t.energy_used)      AS energy_used,
       sum(t.energy_cost)      AS energy_cost,
       sum(t.harsh_brake_count + t.harsh_accel_count) AS harsh_events
FROM trip t JOIN vehicle v ON v.id = t.vehicle_id
GROUP BY 1, 2, 3
WITH NO DATA;
-- @@
CREATE UNIQUE INDEX mv_fleet_cost_daily_pk ON mv_fleet_cost_daily (tenant_id, fleet_id, day);
-- @@
CREATE VIEW v_fleet_cost_daily WITH (security_barrier = true) AS
SELECT * FROM mv_fleet_cost_daily WHERE tenant_id = app_tenant();
