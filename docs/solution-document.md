# Solution Document — FleetSentinel (draft, in the brief's own §1–§14 order)

> The official template is not in the folder yet. This draft follows the problem
> statement's section order 1:1, so every block below moves directly into the template
> when it lands. Numbers are measured; estimates are labelled as estimates.

## 1. Problem framing
Mixed ICE/EV fleets lose money to idling and fuel burn, learn about breakdowns too late,
and cannot coach risky drivers — across 25+ OEM data formats with no common schema.
FleetSentinel turns a 100,000-vehicle telemetry stream into ranked, dollar-quantified
actions within seconds, plus a guardrailed copilot that proposes audited actions.

## 2. Industry context (Motorq as reference, independent work)
Device-free OEM-cloud ingestion (no dongles), normalization engines across brands,
AI layers that flag idling and estimate dollar impact. Motorq is an industry reference
only; no affiliation (see §14 below). Our answers: 4 OEM-cloud connectors with batching,
a declarative mapping engine with live onboarding, cost/maintenance/safety from one stream.

## 3. Architecture
Microservices (gateway, normalizer, processor, lake-sink, api, agent, batch, web) on a
Redpanda event backbone; diagram in `spec.md §4` (Mermaid) and `deploy/helm/`.
Topics in `contracts/topics.yaml` (64 partitions, VIN-keyed). Same Helm chart on kind
and the k3s demo VM (`deploy/helm/fleetsentinel`, `infra/terraform/aws/live`).

## 4. Data engineering
- **Simulator** (`simulator/`): 100k vehicles / 50 tenants, 4 OEM shapes, Markov behaviour,
  bursts, 2% out-of-order, 1% duplicates, GPS faults, clock skew, going-dark, tow, depot
  clusters, schema-drift event; non-leaky wear-state ground truth + incident oracle Parquet.
- **Canonical event**: Protobuf via Schema Registry (BACKWARD), ~100–130 B (~35 B on broker
  with zstd) vs ~1 KB JSON.
- **Polyglot**: Postgres+Timescale+pgvector (OLTP, RLS, rollups, vectors, audit chain),
  MongoDB (twins, DLQ, mapping drafts), Redis (live state, windows, checkpoints, limits),
  MinIO/S3 Parquet lake (Hive `date=/oem=/vehicle_shard=`), DuckDB batch engine.
- **ER diagram**: `spec.md §5.1` (3NF + documented denormalizations). Migrations `db/migrations/0001-0007`.
- **SQL write-up**: `docs/evidence/sql/` — 4 before/after cases with real plans
  (partial index: 3499→93 buffers on counts; matview 40.6→0.07 ms; keyset 25.6→0.03 ms;
  N+1 201q/1091ms→1q/36ms).

## 5. Algorithms
`docs/algorithms.md`: 15-algorithm catalog with complexities; measured A*/Dijkstra parity,
budget-pruned charger search (16 vs 300 expansions), PELT 4.6× faster with identical
changepoints; exact anti-replay dedup rationale.

## 6. System design
Ten named principles mapped in `spec.md §14.1`. Highlights: effectively-once via
per-partition checkpoint-before-commit + idempotent sinks (proven by
`test_checkpoint_before_commit_and_replay_identical_ids`); back-pressure (bounded queue,
spill buffer); circuit breakers; CQRS/event-sourcing (Kafka log = truth, stores are
projections); 5 ADRs in `docs/adr/` + `docs/decisions.md`.

## 7. ML and vector layer
Model card `docs/model-card.md`: LightGBM + isotonic calibration on the 4-feature vector,
temporal + by-VIN splits, PR-AUC/precision@K/Brier/expected-$, robustness at 3 noise levels.
Beats the rule baseline on both splits (p@K 0.14–0.22 vs 0.02–0.08); ties logreg (stated).
Safety score is an explainable per-100km formula with coaching levers. Vectors: 32-dim
vehicle-day signatures (look-alike kNN) + local DTC embeddings.

## 8. Agentic AI
LangGraph copilot, 10-tool allowlist, tenant scope server-side, numeric grounding check,
≤8 calls/turn, retry-then-degrade, pending-only proposals with human approval, full audit.
Rules briefing works with no LLM key. Eval harness: 5 golden + 4 red-team (stub LLM, green).

## 9. NFR evidence
| NFR | Evidence |
|---|---|
| 100k events/s + 3× burst | Measured peak + 2-vs-4-consumer scaling slope on Codespace/VM (`tests/load/k6-api.js`), extrapolated honestly |
| Critical alert < 5 s | Priority lane: per-event rules, ~1 s typical (unit-proven; live trace in demo) |
| Ingest→screen < 2 s | Redis latest-state + WebSocket deltas |
| 99.9% availability | Stateless replicas + PDBs + chaos kill-consumer run (`tests/chaos/kill-consumer.sh`) with 0-loss reconciliation |
| Security | STRIDE (`docs/security/stride.md`), Semgrep/Trivy/gitleaks, BOLA tests on live PG |

## 10. Testing evidence
671 tests (665 passed + 6 live-Postgres tests that pass when Postgres is up), 90% coverage (run 2026-10-02) (unit, live-PG integration, contracts, BDD, evals). Coverage gate ≥80% in CI.
Load/chaos evidence under `docs/evidence/` after the VM runs.

## 11. Security and compliance
OWASP API Top 10 mapping (spec §12.1), crypto-shredding erasure (replay test → `[erased]`,
KEK fail-closed), k-anonymous insight export, hash-chained audit with tamper detection,
synthetic-data-only rule.

## 12. DevOps
Compose (infra/apps/obs/ha profiles), Helm chart, Terraform live (validated; apply for the
demo, destroy after) + reference (plan-only), CI (fast gates) + pr.yml (integration,
contracts, BDD, web, image scan, terraform fmt). Cost model `docs/cost-model.md`
($0.016/vehicle/mo; demo VM $15–25).

## 13. Demo
Storyboard `spec.md §21` (≤5:00, 10 beats). Live URL from the Terraform VM (or Cloudflare
Tunnel fallback — free, see `docs/demo-url-tunnel.md`).

## 14. Rules and declarations
- Data: synthetic only, no real personal/vehicle data. AI-assisted build (this session's
  agents + reviewers, logged in `docs/CHAT_LOG.md`); open-source tools declared in repo
  (uv/pip packages, OSM ODbL data, OBD-II generic descriptions).
- Originality: independent design and code. Motorq affiliation disclaimer: none — industry
  reference only (also in README).
- Code freeze/tag: `v1.0-submission` at submission.

## References
OpenStreetMap (ODbL), generic OBD-II DTC descriptions, US DOE idling-fuel guidance,
AWS us-east-1 list prices, W3C traceparent, STRIDE (Microsoft), OWASP API Top 10 2023 /
LLM Top 10.
