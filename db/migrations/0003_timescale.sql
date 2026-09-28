-- 0003_timescale: hot tier. 1-minute rollups as a hypertable partitioned by TIME (1-day chunks) and VEHICLE (hash dimension),
-- compressed after 2 days, retained 7 days; continuous aggregate for hourly fleet metrics (retained beyond the raw window).
-- Tenant access goes through security-barrier views (below) rather than RLS on the hypertable, because RLS and
-- TimescaleDB compression do not combine on all versions. The views give the same DB-level guarantee.
-- Statements are separated by "-- @@" because CREATE MATERIALIZED VIEW ... WITH (timescaledb.continuous) cannot run in a transaction block.

CREATE TABLE telemetry_1m (
  bucket        timestamptz NOT NULL,
  tenant_id     uuid NOT NULL,
  fleet_id      uuid NOT NULL,
  vehicle_id    uuid NOT NULL,
  samples       int NOT NULL,
  avg_speed_kmh real,
  max_speed_kmh real,
  distance_km   real NOT NULL DEFAULT 0,
  idle_s        real NOT NULL DEFAULT 0,
  idle_cost     numeric(10,4) NOT NULL DEFAULT 0,       -- USD, leakage while idling
  harsh_brake   smallint NOT NULL DEFAULT 0,
  harsh_accel   smallint NOT NULL DEFAULT 0,
  overspeed_s   real NOT NULL DEFAULT 0,
  min_soc       real,
  max_soc       real,
  dtc_count     smallint NOT NULL DEFAULT 0,
  last_lat      double precision,
  last_lon      double precision
);
-- @@
SELECT create_hypertable('telemetry_1m', 'bucket', chunk_time_interval => INTERVAL '1 day');
-- @@
SELECT add_dimension('telemetry_1m', 'vehicle_id', number_partitions => 8);
-- @@
CREATE UNIQUE INDEX telemetry_1m_vehicle_bucket ON telemetry_1m (vehicle_id, bucket);   -- enables idempotent UPSERT of a window
-- @@
CREATE INDEX telemetry_1m_tenant_bucket ON telemetry_1m (tenant_id, bucket DESC);
-- @@
ALTER TABLE telemetry_1m SET (timescaledb.compress, timescaledb.compress_segmentby = 'vehicle_id', timescaledb.compress_orderby = 'bucket DESC');
-- @@
SELECT add_compression_policy('telemetry_1m', INTERVAL '2 days');
-- @@
SELECT add_retention_policy('telemetry_1m', INTERVAL '7 days');
-- @@
CREATE MATERIALIZED VIEW fleet_metrics_1h WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 hour', bucket) AS hour,
       tenant_id, fleet_id,
       sum(idle_s)::double precision      AS idle_s,
       sum(idle_cost)                     AS idle_cost,
       sum(distance_km)::double precision AS distance_km,
       sum(harsh_brake)::bigint           AS harsh_brake,
       sum(harsh_accel)::bigint           AS harsh_accel,
       count(*)                           AS vehicle_minutes
FROM telemetry_1m
GROUP BY 1, 2, 3
WITH NO DATA;
-- @@
SELECT add_continuous_aggregate_policy('fleet_metrics_1h',
       start_offset => INTERVAL '3 days', end_offset => INTERVAL '1 minute', schedule_interval => INTERVAL '1 minute');
-- @@
CREATE VIEW v_telemetry_1m WITH (security_barrier = true) AS
SELECT * FROM telemetry_1m WHERE tenant_id = app_tenant();
-- @@
CREATE VIEW v_fleet_metrics_1h WITH (security_barrier = true) AS
SELECT * FROM fleet_metrics_1h WHERE tenant_id = app_tenant();
