-- 0001_core: extensions + relational core (3NF).
-- Conventions:
--   * Every tenant-scoped table carries tenant_id (deliberate, documented denormalization: RLS predicate + index locality).
--     Consistency with the owning parent is enforced with COMPOSITE FKs (tenant_id, parent_id) -> parent(tenant_id, id),
--     so a row can never point at a parent in another tenant.
--   * All money is USD (tenant.currency is display-only). Tariffs are USD-equivalent.
--   * Deterministic ids (alert, trip) are supplied by the writer (uuid5), making replays idempotent (ON CONFLICT DO NOTHING).

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ---------- global reference data (no tenant_id) ----------
CREATE TABLE oem (
  id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code text NOT NULL UNIQUE,                 -- OEM_A .. OEM_D
  name text NOT NULL
);

CREATE TABLE vehicle_model (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  oem_id             uuid NOT NULL REFERENCES oem(id),
  name               text NOT NULL,
  powertrain         text NOT NULL CHECK (powertrain IN ('ICE','EV','HYBRID')),
  battery_kwh        numeric(6,1),                     -- EV/hybrid only
  idle_fuel_l_per_h  numeric(6,3),                     -- ICE/hybrid idle burn
  idle_kw            numeric(6,3),                     -- EV idle draw (HVAC etc.)
  UNIQUE (oem_id, name)
);

CREATE TABLE dtc_code (
  code        char(5) PRIMARY KEY CHECK (code ~ '^[PBCU][0-9A-F]{4}$'),
  system      text NOT NULL CHECK (system IN ('powertrain','body','chassis','network')),
  generic     boolean NOT NULL,
  description text,
  critical    boolean NOT NULL DEFAULT false
);

CREATE TABLE tariff_region (
  id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code     text NOT NULL UNIQUE,
  name     text NOT NULL,
  currency char(3) NOT NULL DEFAULT 'USD'
);

CREATE TABLE tariff_rate (             -- time-of-use price per energy type
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tariff_region_id uuid NOT NULL REFERENCES tariff_region(id),
  energy_type      text NOT NULL CHECK (energy_type IN ('DIESEL','PETROL','ELECTRICITY')),
  hour_from        smallint NOT NULL CHECK (hour_from BETWEEN 0 AND 23),
  hour_to          smallint NOT NULL CHECK (hour_to BETWEEN 1 AND 24),
  price_per_unit   numeric(10,4) NOT NULL CHECK (price_per_unit >= 0),
  unit             text NOT NULL CHECK (unit IN ('L','kWh')),
  UNIQUE (tariff_region_id, energy_type, hour_from)
);

CREATE TABLE charger (
  id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name      text NOT NULL,
  lat       double precision NOT NULL,
  lon       double precision NOT NULL,
  kw        numeric(6,1) NOT NULL,
  connector text NOT NULL DEFAULT 'CCS2'
);

CREATE TABLE model_version (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name         text NOT NULL,                          -- maintenance_risk | safety_score
  version      text NOT NULL,
  trained_at   timestamptz NOT NULL DEFAULT now(),
  metrics      jsonb NOT NULL DEFAULT '{}',            -- pr_auc, precision_at_k, brier, ... per split
  artifact_uri text,
  is_active    boolean NOT NULL DEFAULT false,
  UNIQUE (name, version)
);
CREATE UNIQUE INDEX model_version_one_active ON model_version (name) WHERE is_active;

CREATE TABLE schema_mapping (          -- declarative OEM mappings (see contracts/mappings/README.md); immutable once active
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  oem_id       uuid NOT NULL REFERENCES oem(id),
  version      int  NOT NULL,
  status       text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved','active','retired')),
  document     jsonb NOT NULL,
  created_by   text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  activated_at timestamptz,
  UNIQUE (oem_id, version)
);
CREATE UNIQUE INDEX schema_mapping_one_active ON schema_mapping (oem_id) WHERE status = 'active';

-- ---------- tenant-scoped core ----------
CREATE TABLE tenant (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL UNIQUE,
  data_region text NOT NULL DEFAULT 'ap-south-1',
  currency    char(3) NOT NULL DEFAULT 'USD',
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE subscription (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenant(id),
  plan        text NOT NULL CHECK (plan IN ('starter','growth','enterprise')),
  seats       int  NOT NULL CHECK (seats > 0),
  status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active','past_due','cancelled')),
  renewed_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE user_account (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenant(id),
  keycloak_sub  text NOT NULL UNIQUE,
  email         text NOT NULL,
  role          text NOT NULL CHECK (role IN ('platform_admin','fleet_admin','analyst','viewer','lender_agent')),
  UNIQUE (tenant_id, id)
);

CREATE TABLE fleet (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenant(id),
  tariff_region_id uuid NOT NULL REFERENCES tariff_region(id),
  name             text NOT NULL,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, name)
);

CREATE TABLE vehicle (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenant(id),
  fleet_id        uuid NOT NULL,
  model_id        uuid NOT NULL REFERENCES vehicle_model(id),
  vin             char(17) NOT NULL UNIQUE CHECK (vin ~ '^[A-HJ-NPR-Z0-9]{17}$'),
  last_risk_score numeric(4,3) NOT NULL DEFAULT 0,   -- DENORMALIZED cached projection (<=60s stale), rebuildable from the log
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, fleet_id) REFERENCES fleet (tenant_id, id)
);
CREATE INDEX vehicle_fleet_idx ON vehicle (fleet_id);

-- Crypto-shredding key store (ADR-04): one random 256-bit data key (DEK) per data subject, stored ONLY wrapped by a
-- Vault Transit KEK. PII (driver name/license, driver_ref in events) is AES-256-GCM encrypted under the DEK.
-- Erasure = set wrapped_dek NULL (+ destroyed_at): every ciphertext copy (Postgres, Kafka, lake, backups) becomes unreadable.
CREATE TABLE subject_key (
  subject_key_id text PRIMARY KEY,
  tenant_id      uuid NOT NULL REFERENCES tenant(id),
  wrapped_dek    bytea,
  created_at     timestamptz NOT NULL DEFAULT now(),
  destroyed_at   timestamptz,
  CHECK ((wrapped_dek IS NULL) = (destroyed_at IS NOT NULL))
);

CREATE TABLE driver (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid NOT NULL REFERENCES tenant(id),
  fleet_id       uuid NOT NULL,
  name_enc       bytea,                        -- AES-256-GCM under the subject DEK; NULLed on erasure
  license_enc    bytea,                        -- ciphertext of a SIMULATOR-GENERATED fake license string
  subject_key_id text NOT NULL UNIQUE REFERENCES subject_key(subject_key_id),
  safety_score   numeric(5,2),                 -- DENORMALIZED cached projection
  safety_confidence text CHECK (safety_confidence IN ('low','ok')),
  erased_at      timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, fleet_id) REFERENCES fleet (tenant_id, id)
);

CREATE TABLE driver_assignment (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL,
  vehicle_id    uuid NOT NULL,
  driver_id     uuid NOT NULL,
  valid_during  tstzrange NOT NULL,
  FOREIGN KEY (tenant_id, vehicle_id) REFERENCES vehicle (tenant_id, id),
  FOREIGN KEY (tenant_id, driver_id)  REFERENCES driver (tenant_id, id),
  EXCLUDE USING gist (vehicle_id WITH =, valid_during WITH &&)   -- a vehicle has at most one driver at a time
);

CREATE TABLE depot (
  id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  fleet_id  uuid NOT NULL,
  name      text NOT NULL,
  lat       double precision NOT NULL,
  lon       double precision NOT NULL,
  radius_m  int NOT NULL DEFAULT 300,
  FOREIGN KEY (tenant_id, fleet_id) REFERENCES fleet (tenant_id, id)
);

CREATE TABLE alert_rule (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenant(id),
  fleet_id    uuid,
  code        text NOT NULL,                   -- TOW, CRASH, EXCESSIVE_IDLING, ... (see alert.proto RuleCode, without RULE_ prefix)
  severity    text NOT NULL CHECK (severity IN ('INFO','WARNING','CRITICAL')),
  config      jsonb NOT NULL DEFAULT '{}',
  cooldown_s  int  NOT NULL DEFAULT 900,
  enabled     boolean NOT NULL DEFAULT true,
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, fleet_id) REFERENCES fleet (tenant_id, id)
);

CREATE TABLE trip (
  id                 uuid PRIMARY KEY,         -- deterministic: fscore.contracts.trip_id(vin, start_ts_ms)
  tenant_id          uuid NOT NULL,
  vehicle_id         uuid NOT NULL,
  driver_id          uuid,
  start_ts           timestamptz NOT NULL,
  end_ts             timestamptz NOT NULL CHECK (end_ts >= start_ts),
  distance_km        numeric(9,2) NOT NULL DEFAULT 0,
  idle_s             int NOT NULL DEFAULT 0,
  energy_used        numeric(9,2) NOT NULL DEFAULT 0,      -- litres (ICE) or kWh (EV)
  energy_cost        numeric(10,2) NOT NULL DEFAULT 0,     -- DENORMALIZED derived fact (recomputed nightly from the lake)
  harsh_brake_count  int NOT NULL DEFAULT 0,
  harsh_accel_count  int NOT NULL DEFAULT 0,
  overspeed_s        int NOT NULL DEFAULT 0,
  night_share        numeric(4,3) NOT NULL DEFAULT 0,
  FOREIGN KEY (tenant_id, vehicle_id) REFERENCES vehicle (tenant_id, id),
  FOREIGN KEY (tenant_id, driver_id)  REFERENCES driver (tenant_id, id)
);
CREATE INDEX trip_vehicle_idx ON trip (vehicle_id);      -- baseline (naive) index; see 0006 for the optimised ones

CREATE TABLE alert (
  id                 uuid PRIMARY KEY,         -- deterministic: fscore.contracts.alert_id(vin, rule, window_start_ms)
  tenant_id          uuid NOT NULL,
  vehicle_id         uuid NOT NULL,
  rule_id            uuid,
  rule_code          text NOT NULL,
  dtc_code           char(5) REFERENCES dtc_code(code),
  severity           text NOT NULL CHECK (severity IN ('INFO','WARNING','CRITICAL')),
  status             text NOT NULL DEFAULT 'open' CHECK (status IN ('open','ack','resolved')),
  occurrences        int  NOT NULL DEFAULT 1,
  impact_at_creation numeric(12,2) NOT NULL DEFAULT 0,  -- point-in-time fact, deliberately not recomputed
  opened_at          timestamptz NOT NULL,
  event_ts           timestamptz,
  detected_ts        timestamptz,
  ingest_ts          timestamptz,
  lat                double precision,
  lon                double precision,
  summary            text,
  details            jsonb NOT NULL DEFAULT '{}',
  trace_id           text,
  acknowledged_by    uuid,
  resolved_at        timestamptz,
  FOREIGN KEY (tenant_id, vehicle_id) REFERENCES vehicle (tenant_id, id),
  FOREIGN KEY (tenant_id, rule_id)    REFERENCES alert_rule (tenant_id, id)
);
CREATE INDEX alert_vehicle_idx ON alert (vehicle_id);    -- baseline

CREATE TABLE risk_prediction (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL,
  vehicle_id       uuid NOT NULL,
  model_version_id uuid NOT NULL REFERENCES model_version(id),
  p_failure_7d     numeric(5,4) NOT NULL CHECK (p_failure_7d BETWEEN 0 AND 1),
  reason_codes     jsonb NOT NULL DEFAULT '[]',            -- SHAP top contributors
  predicted_at     timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, vehicle_id) REFERENCES vehicle (tenant_id, id)
);
CREATE INDEX risk_prediction_vehicle_idx ON risk_prediction (vehicle_id, predicted_at DESC);

CREATE TABLE agent_action (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL REFERENCES tenant(id),
  alert_id            uuid REFERENCES alert(id),
  reviewer_id         uuid,
  status              text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  proposal            jsonb NOT NULL,
  rationale           text,
  expected_impact_usd numeric(12,2) NOT NULL DEFAULT 0,
  created_at          timestamptz NOT NULL DEFAULT now(),
  decided_at          timestamptz,
  decided_reason      text,
  idempotency_key     text,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, idempotency_key),
  FOREIGN KEY (tenant_id, reviewer_id) REFERENCES user_account (tenant_id, id)
);

CREATE TABLE work_order (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL,
  agent_action_id uuid NOT NULL UNIQUE,
  state           text NOT NULL DEFAULT 'open' CHECK (state IN ('open','in_progress','done','cancelled')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, agent_action_id) REFERENCES agent_action (tenant_id, id)
);

CREATE TABLE dsr_request (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL,
  driver_id    uuid NOT NULL,
  kind         text NOT NULL CHECK (kind IN ('ERASE','ACCESS')),
  status       text NOT NULL DEFAULT 'received' CHECK (status IN ('received','verified','completed','rejected')),
  requested_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  evidence     jsonb NOT NULL DEFAULT '{}',
  FOREIGN KEY (tenant_id, driver_id) REFERENCES driver (tenant_id, id)
);

CREATE TABLE consent_record (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL,
  driver_id   uuid NOT NULL,
  purpose     text NOT NULL,
  granted     boolean NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, driver_id) REFERENCES driver (tenant_id, id)
);

-- ---------- vector layer (pgvector) ----------
CREATE TABLE vehicle_signature (       -- 32-dim normalised vehicle-day telemetry signature for look-alike search
  vehicle_id uuid NOT NULL,
  tenant_id  uuid NOT NULL,
  day        date NOT NULL,
  embedding  vector(32) NOT NULL,
  failed_7d  boolean,                  -- NULL when unknown (future); true/false from oracle/labels for history
  PRIMARY KEY (vehicle_id, day),
  FOREIGN KEY (tenant_id, vehicle_id) REFERENCES vehicle (tenant_id, id)
);
CREATE INDEX vehicle_signature_hnsw ON vehicle_signature USING hnsw (embedding vector_cosine_ops);

CREATE TABLE knowledge_chunk (         -- DTC knowledge base + resolved incidents; tenant_id NULL = global
  id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid REFERENCES tenant(id),
  kind      text NOT NULL CHECK (kind IN ('dtc','incident')),
  ref       text NOT NULL,
  content   text NOT NULL,
  embedding vector(384) NOT NULL       -- all-MiniLM-L6-v2 (local, via fastembed)
);
CREATE INDEX knowledge_chunk_hnsw ON knowledge_chunk USING hnsw (embedding vector_cosine_ops);
