# Parallel build streams (contract-first)

Spec: `spec.md` v3. Frozen contracts (do NOT change without asking the integrator): `contracts/proto/**`, `contracts/topics.yaml`,
`contracts/openapi.yaml`, `contracts/mappings/**` (DSL + samples), `db/migrations/000{1..7}_*.sql`, `libs/fscore/src/fscore/{contracts,vin,dtc,hashing,mapping_engine}.py`.
If a contract is wrong or missing something, STOP and report it instead of working around it.

## Rules for every stream
1. **Own only your directories.** Never edit another stream's files. Shared files (`pyproject.toml`, `uv.lock`, `docker-compose.yml`, `Makefile`, CI) are the integrator's: ask.
2. **Do not commit or push.** The integrator commits. Do not run `git` write commands.
3. **Deps are pre-declared** in each package's `pyproject.toml`. Use `uv run --frozen ...`. Need a new dependency? Ask, do not `uv add`.
4. **Tests are mandatory.** Core/domain/application code >= 80% unit coverage (I/O adapters live under `adapters/` or `main.py`, which are omitted from the gate and covered by integration tests). Add Hypothesis property tests for algorithms.
5. **Memory is scarce on this machine (about 13.9 GB, often < 1 GB free).** Never start the whole compose stack. Prefer unit tests. If you need a broker/DB, start only that one container (`docker compose -f deploy/compose/docker-compose.yml up -d <service>`) and stop it when done. Never run more than one heavy process at a time.
6. **Report honestly.** Measured numbers only. If a target is missed, say so with the measurement.
7. Python 3.12, type hints, structlog JSON logs, no per-event logging (log per batch/errors), Prometheus metrics named as in spec section 14.5.

## Stream A: simulator + seed (`simulator/`, `tests/e2e_oracle/` helpers)
Deliver `fs_simulator`: (1) 4 OEM connectors publishing batches over mTLS MQTT (`telemetry/{oem}/{shard}` bulk; `event/{oem}/{shard}` for critical events + watchlist telemetry), payloads matching `contracts/mappings/samples/*`, certs from `deploy/certs`;
(2) per-vehicle Markov behaviour (parked/driving/idling/charging), OSM route library (offline pre-process once; commit the compressed artifact; fall back to a synthetic grid) ; (3) faults: 3x bursts, ~2% out-of-order, ~1% duplicates (record + whole-batch), GPS jumps, stuck sensor, odometer rollback, clock skew, going dark, tow, depot clusters, and the **OEM_D schema-drift event** (switches to the columnar format mid-run on a trigger);
(4) **non-leaky maintenance ground truth** per spec 6.1 (hidden wear state; features only from data before prediction time);
(5) `ground_truth` Parquet oracle of every injected incident + a per-VIN sent-seq ledger for the reconciliation report; (6) `make seed`: 100k vehicles / 50 tenants from `contracts/demo_tenants.yaml` into Postgres (fs_worker role), encrypted fake driver PII (AES-GCM DEK wrapped via Vault Transit batch), `lite` and `full` (~1B row) lake profiles written as Parquet to MinIO.
Done when: publishes >= 25k events/s per process (report measured), seed lite < 10 min, all VINs valid, ground truth written, unit tests >= 80% on generators.

## Stream B: ingestion + stream processing (`services/gateway`, `normalizer`, `processor`, `lake_sink`)
Gateway (bounded queue back-pressure, exact + Bloom-negative batch dedup, idempotent producer, disk spill buffer, routes `event/*` to `ingest.priority.v1`); normalizer (mapping engine + hot reload from `config.mappings.v1`, DLQ, drift detection, driver_ref encryption, **batch-vectorised fast path, target >= 40k rec/s/core**; own group for the priority lane);
processor: priority lane rules (TOW, CRASH, CRITICAL_DTC, watchlist GEOFENCE, LOW_SOC_STRANDING via A*, GOING_DARK via hierarchical timing wheel in EVENT time) and bulk lane (60 s event-time windows, exact anti-replay dedup, Count-Min top-K, HyperLogLog, geohash + Union-Find depot clusters, trip state machine, idling cost using tariffs, ML scoring with rule-baseline fallback), checkpointing to Redis then offset commit, deterministic ids + `ON CONFLICT DO NOTHING`; lake sink writing offset-named Parquet. Algorithms go in `libs/fscore/src/fscore/algorithms` + `sketches` (you own those two packages).
Done when: unit tests >= 80% on domain code, Hypothesis tests for dedup/CMS/timing wheel, replay after a kill produces identical alert ids, alert latency measured with the oracle.

## Stream C: API + security (`services/api`, `libs/fscore/src/fscore/crypto.py`)
FastAPI implementing `contracts/openapi.yaml` exactly (contract test: response models match), Keycloak JWT validation (RS256, iss/aud/exp), per-transaction `set_config('app.tenant_id')`, RBAC deny-by-default, audit middleware on EVERY request (emit `audit.events.v1`), keyset pagination, Redis rate limits, Idempotency-Key handling, circuit breakers, WebSocket `/v1/stream`, DSR crypto-shredding erasure (Vault Transit unwrap/destroy), audit hash-chain verify, privacy export with k-anonymity, mapping lifecycle endpoints, audit sink consumer.
Done when: BOLA test (tenant A cannot read tenant B) passes against real Postgres RLS, erasure test proves replay yields unreadable PII, unit >= 80% on services/application code, OpenAPI conformance test green.

## Stream D: intelligence (`ml/`, `services/agent`, `services/batch`)
DuckDB feature builder over the lake; LightGBM + isotonic calibration; baselines (rule, logistic); PR-AUC, precision@K, Brier, expected-$ on temporal AND by-VIN splits, robustness curve at 3 noise levels, model card; cost-optimal inspection list; explainable safety score per 100 km with coaching levers; 32-dim vehicle-day signatures + pgvector look-alike kNN; local embeddings (fastembed) for the DTC knowledge base;
copilot: LangGraph agent with the allowlisted read-only tools, pseudonymization before the LLM, numeric-grounding check, retry/degrade, `propose_action`, proactive briefing (+ rules-generated fallback when no LLM), mapping drafting, evaluation harness (30 golden + 12 red-team) with a stub LLM for CI.
Done when: model beats both baselines on both splits (report honestly if not), guardrail tests green with the stub LLM, briefing works with no API key.

## Stream E: web (`web/`)
React + Vite + TypeScript + Tailwind + shadcn/ui + TanStack Query + deck.gl on MapLibre with a self-hosted PMTiles Bengaluru basemap. Build against a mock server generated from `contracts/openapi.yaml` first, then switch to the real API. Screens: Command Center (live 100k-vehicle map, KPI strip with measured freshness), Vehicle 360, Drivers, Copilot (chat, briefing, approval queue), Platform (mapping editor with live preview, DLQ, data quality), Compliance (audit with verify-chain, DSR, export). OIDC Authorization Code + PKCE. WCAG AA, keyboard navigable, designed empty/loading/error states.
Done when: all screens on the real API, Vitest for logic, typecheck + eslint clean, map holds 60 fps with 100k points.

## Stream F: DevOps + QA (`deploy/`, `infra/`, `tests/`, `.github/`, `docs/evidence`)
Compose profiles `apps`, `obs`, `ha` (3 brokers RF=3); Dockerfiles; Helm umbrella chart (probes on real dependencies, PDBs, NetworkPolicies, securityContext, HPA); Terraform `aws/live` (VPC, one EC2 + k3s, S3 SSE-KMS, object-locked anchor bucket, budget alarm; applied for the demo) and `aws/reference` (plan-validated); Prometheus/Loki/Tempo/Grafana with SLO dashboards; CI `pr.yml` (Testcontainers integration, Pact contracts x2, BDD `behave` x5, compliance, chaos scripts, Trivy + SBOM, ZAP baseline + API scan); k6 + simulator load/burst/soak scripts; reconciliation job; STRIDE model `docs/security/stride.md`; cost model `docs/cost-model.md`; SQL write-up `docs/sql-writeup.md` (four before/after EXPLAIN ANALYZE cases via `fs migrate --to 5`); algorithms write-up `docs/algorithms.md`.
Done when: CI green on a PR, live URL up on the cloud VM, chaos + load evidence committed under `docs/evidence/`.
