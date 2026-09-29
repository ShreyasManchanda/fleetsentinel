# STRIDE threat model — FleetSentinel

Scope: the 6 trust boundaries in spec §13.6. Each row: threat → control → where it is
proven (test, migration, or accepted residual risk). Residual risks are stated, not hidden.

## 1. OEM connector → MQTT (Mosquitto, mTLS)

| # | STRIDE | Threat | Control | Proof |
|---|---|---|---|---|
| 1.1 | Spoofing | Rogue publisher impersonates an OEM connector | mTLS: one client cert per OEM connector, CN = username; topic ACLs (`telemetry/{oem}/#` only its own prefix) | `deploy/mosquitto/`, `deploy/certs/`; B-009 notes |
| 1.2 | Tampering | Malformed/oversize batch corrupts the pipeline | Envelope checks (≤1 MB, known OEM, record count) before Kafka produce | `services/gateway` unit tests (33 green) |
| 1.3 | Repudiation | Connector denies sending a batch | `batch_id` + `received_ts_ms` headers preserved end to end; DLQ keeps raw bytes + reason | spill/DLQ tests |
| 1.4 | Info. disclosure | Telemetry sniffed on the wire | TLS 1.3 (mTLS); PII never plaintext in the log (`driver_ref_enc`) | contracts/proto |
| 1.5 | DoS | 3× burst overwhelms the gateway | Bounded `asyncio.Queue` (10k) + MQTT back-pressure + on-disk spill buffer with ordered replay | spill tests, B-016 |
| 1.6 | Elevation | Connector publishes to another OEM's topic | ACL deny-by-default per client cert | mosquitto ACL file |

## 2. Gateway → Kafka (internal backbone)

| # | STRIDE | Threat | Control | Proof |
|---|---|---|---|---|
| 2.1 | Tampering | Batch modified/lost between gateway and log | Idempotent producer (`enable.idempotence`, `acks=all`, zstd); spill buffer replays in order | gateway tests |
| 2.2 | Info. disclosure | PLAINTEXT Kafka listeners in compose | **[DESIGNED]** SASL/ACLs on the broker (compose is a trusted network; production Helm enables SASL) | residual risk §R-3 |
| 2.3 | DoS | Unbounded consumer lag | 64 partitions (headroom for 3× burst); lag metrics; priority lane has its own group | spec §4 |

## 3. Browser → API (FastAPI, Keycloak OIDC)

| # | STRIDE | Threat | Control | Proof |
|---|---|---|---|---|
| 3.1 | Spoofing | Forged/expired tokens | RS256 pinned, `iss`/`aud`/`exp`/`nbf` validated, JWKS cached with rotation | `jwt_auth.py`, unit tests |
| 3.2 | Tampering | Client-supplied tenant escalation (BOLA) | Tenant from JWT only; `set_config('app.tenant_id')` per txn + Postgres RLS (hard guarantee, holds for raw psql) | `tests/integration/test_pg_security.py` (B-019/B-022, live PG) |
| 3.3 | Repudiation | "I never approved that action" | Every request emits `audit.events.v1` → hash-chained `audit_log`; `audit_verify()` detects tampering | integration test (tamper → `first_bad_seq`) |
| 3.4 | Info. disclosure | Excessive data exposure / mass assignment | Explicit pydantic response models; role-based field masking (location precision by role) | contract tests |
| 3.5 | DoS | Quota exhaustion, deep pagination | Redis token buckets (tenant/user/agent), page cap 200, keyset pagination (no OFFSET), 1 MB body cap, query timeouts | k6 abuse scenario (T2) |
| 3.6 | Elevation | VIEWER calls admin/DSR endpoints | Per-route role guards, deny-by-default; DSR requires `Perm.DSR` | `test_api_domain.py:73` + BOLA tests |

## 4. Agent → LLM provider (external)

| # | STRIDE | Threat | Control | Proof |
|---|---|---|---|---|
| 4.1 | Info. disclosure (data) | Driver PII / precise location leaks into prompts | Pseudonymization before the LLM (opaque driver tokens; geohash-6 locations); re-identify in UI only | spec §11.3 |
| 4.2 | Info. disclosure (cross-tenant) | Prompt asks for another tenant's data | Tenant scope injected server-side into every tool call, never from LLM output; RLS backstop | red-team eval set (12 prompts, must be 100% block) |
| 4.3 | Tampering (prompt injection) | Malicious instruction inside OEM free-text / payload | Tool outputs passed as data blocks, never instructions; `drop_unmapped` strips free text; numeric grounding check (every number must appear in a tool result) | eval harness §11.4 |
| 4.4 | Elevation (agency) | Agent approves its own work order | Tools read-only; only `propose_action` writes, always `pending`; human approval carries idempotency key | `AGENT_ACTION` lifecycle |
| 4.5 | DoS | Unbounded token spend | ≤8 tool calls/turn, 30 s timeout, per-tenant daily token budget, 20 req/min/user | config |

## 5. Admin / mapping activation (new-OEM onboarding)

| # | STRIDE | Threat | Control | Proof |
|---|---|---|---|---|
| 5.1 | Tampering | Malicious mapping executes code | No `eval`: transforms are a fixed allowlist; unknown keys rejected at compile (`MappingError`) | `test_mapping_safety_rules` |
| 5.2 | Tampering | Drifted/typo'd event codes silently reclassified | `enum_map` fails unknown non-empty codes to DLQ (B-021); bare `ON`/`OFF` YAML booleans fixed by quoting | regression tests |
| 5.3 | Elevation | Any user activates a mapping | `platform_admin` role only; draft → validate → preview → approve → activate; activation published to compacted topic, hot-reloaded | Platform screen |
| 5.4 | Repudiation | "I didn't activate that mapping" | Activation is an audited admin action (actor, tenant NULL, platform scope) | audit log |

## 6. Data stores (Postgres, Mongo, Redis, lake)

| # | STRIDE | Threat | Control | Proof |
|---|---|---|---|---|
| 6.1 | Info. disclosure | PII readable in DB/lake/backups/Kafka replays | Envelope encryption (per-subject DEK, Vault Transit KEK); erasure = destroy wrapped DEK (crypto-shredding, ADR-04) | erasure replay test → `[erased]` (B-022); KEK fail-closed (B-020) |
| 6.2 | Tampering | Audit trail edited to hide an action | Append-only triggers + hash chain + `audit_verify()`; anchors to WORM bucket | tamper test (B-022) |
| 6.3 | Info. disclosure | Tenant A reads tenant B (DB layer) | RLS on 20 tables + `FORCE RLS`; hypertable behind security-barrier view (B-006) | live-PG tests (B-019/B-022) |
| 6.4 | DoS | Hot tenant (30k vehicles) skews partitions | VIN-keyed partitioning spreads tenants; `partition_skew_ratio` metric | spec ADR-02 |
| 6.5 | Repudiation | Lake object silently replaced | Offset-derived object names (rewrite = identical bytes); S3/MinIO PUT atomicity | idempotent-rewrite test |

## Residual risks (accepted, with reason)

| # | Risk | Why accepted | Mitigation path |
|---|---|---|---|
| R-1 | Compose Postgres/Mongo/Redis are single-node | Laptop + demo scope; K8s `ha` profile exists | CloudNativePG replica is T2; backups before demo |
| R-2 | `LocalDevWrapper`/`LocalDevKek` exist in the tree | Needed for CI/unit tests without Vault | Fail-closed wiring (B-020); `INSECURE` markers; never used unless `--local` |
| R-3 | No SASL on compose Kafka | Trusted local network for the demo | Helm enables SASL/SCRAM; Terraform security groups |
| R-4 | ML model trained on synthetic data proves method, not real-world accuracy | Simulator is the only data source | Model card states this openly (spec §10.1) |
