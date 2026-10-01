# FleetSentinel — Solution Document

### Connected Vehicle Intelligence Hackathon (Motorq industry reference)

> **Affiliation disclaimer:** Motorq is used only as an industry reference. This is an independent academic exercise with no affiliation to Motorq.

---

## 1. Problem framing

Mixed ICE/EV fleets lose money every day in three quiet ways: vehicles idle and burn fuel without anyone noticing, mechanical failures surface only after they become breakdowns, and risky driving goes uncoached because no one is scoring it. The data needed to catch all three already exists inside the vehicles — the problem is that it arrives in more than 25 different OEM telemetry formats with no common schema, so most fleet tools either integrate shallowly with one OEM or never attempt the merge at all.

FleetSentinel takes a different approach: one canonical telemetry pipeline for a 100,000-vehicle mixed fleet, feeding three outputs from the same stream — idling/fuel cost leakage, predictive maintenance, and driver safety scoring — turned into ranked, dollar-quantified actions, typically within seconds. A guardrailed AI copilot sits on top, turning those ranked actions into a daily briefing of audited, human-approved work.

## 2. Industry context and trends

The reference company's publicly described model is device-free: it ingests telemetry from OEM clouds rather than installing aftermarket dongles, normalizes across brands, and layers AI on top to flag idling and estimate its dollar impact. FleetSentinel mirrors that shape with its own independent design: four simulated OEM-cloud connectors (each with its own payload format) feeding a declarative mapping engine that can onboard a new OEM live, and a single stream that drives cost, maintenance, and safety analysis together rather than as three separate products.

The brief frames five industry trends running through 2030 — data volume growth requiring edge filtering, software-defined vehicles whose formats change over OTA updates, electrification, cloud-agnostic pressure, agentic AI, and tightening regulation (UNECE R155/R156, DPDP, GDPR). Each has a concrete answer in the design: Protobuf on the wire is roughly 8× smaller than the brief's raw-JSON baseline and the lake downsamples before cold storage; mappings are declarative and versioned, with drift detection correlated to `sw_version`; the fleet mix is ICE/EV/hybrid with SoC-aware charger routing; one Helm chart runs unchanged on a local `kind` cluster and a cloud VM; the copilot ships with proactive briefings, approval gates, an evaluation harness, and a full audit trail; and the security section below maps directly onto the regulatory list.

Eight problem spaces were available in the brief, and all eight could have been built as shallow, independent demos. Instead, fuel/idling cost, predictive maintenance, and driver safety were kept as the three core outputs because they all consume the same canonical event and the same pipeline — one system at full depth instead of eight at shallow depth, which is the brief's own stated preference ("depth beats breadth"). Asset recovery (tow/theft detection) became the showcase for the sub-5-second critical-alert requirement; multi-OEM normalization became the platform backbone and a scripted demo hero moment; privacy-safe sharing and EV charging became supporting features built on the same data.

## 3. Architecture

The system is a set of microservices — gateway, normalizer, processor, lake-sink, api, agent, batch, and a React web console — connected by a Redpanda event backbone. The full architecture diagram (Mermaid) lives in `spec.md §4`, and the deployable form is the Helm chart in `deploy/helm/fleetsentinel`, which runs unchanged on a local `kind` cluster and on the k3s demo VM provisioned by `infra/terraform/aws/live`.

Topics are defined in `contracts/topics.yaml` with 64 partitions, keyed by VIN so that per-vehicle ordering survives partitioning and a single hot tenant cannot skew the whole topic. Ingestion follows the reference company's own domain model rather than a naive one: instead of 100,000 vehicles each holding a direct MQTT connection (which runs into file-descriptor limits and makes per-vehicle mTLS impractical at scale), four simulated OEM-cloud connectors each own a share of the fleet and publish in batches of roughly 100 records or 250 ms, whichever comes first, over mutual-TLS MQTT with per-connector topic ACLs. That single modeling choice turns 100,000 potential connections into 4–16, and turns 100,000 events/s into roughly 1,000 MQTT messages/s (about 3,000 during a 3× burst) — a load Mosquitto handles comfortably. It also means the brief's mTLS requirement is met at the real device-facing trust boundary of a device-free platform (the OEM connector), while direct-device mTLS at 100k scale is documented as the aftermarket/legacy-hardware path and left as a specified-but-not-running design (the brief's "fleet management companies migrating from legacy hardware" case).

## 4. Data engineering

The simulator (`simulator/`) generates 100,000 vehicles across 50 tenants and four distinct OEM payload shapes, with Markov-chain driving behaviour, realistic faults (2% out-of-order delivery, 1% duplicate records, GPS faults, clock skew, vehicles going dark, tow events, depot clustering) and a live schema-drift scenario for the onboarding demo. It also produces a non-leaky wear-state ground truth and an incident oracle in Parquet, so model evaluation is checkable rather than hand-waved.

The canonical event is Protobuf, versioned through a Schema Registry under BACKWARD compatibility, at roughly 100–130 bytes per event (about 35 bytes on the broker after zstd batch compression) — a large reduction against the brief's own ~1 KB raw-JSON reference point. Storage is deliberately polyglot and each store is justified by its own access pattern: PostgreSQL with TimescaleDB and pgvector handles OLTP, row-level tenant security, time-series rollups, vehicle-similarity vectors, and the hash-chained audit trail; MongoDB holds vehicle digital twins, dead-letter-queue payloads, and OEM mapping drafts — a much smaller role than "all telemetry," which the project's own v3 revision identified as an earlier design mistake (§25 below); Redis holds live state, sliding windows, stream checkpoints, and rate limits; and MinIO/S3 holds the Parquet telemetry lake, partitioned by `date=/oem=/vehicle_shard=`, built to scale past a billion rows. The entity-relationship diagram is in `spec.md §5.1` (third normal form, with denormalizations documented rather than accidental), and the schema evolves through seven ordered migrations in `db/migrations/0001-0007`.

Four SQL optimization cases with real `EXPLAIN ANALYZE` plans are recorded in `docs/evidence/sql/` and were re-generated on real data rather than estimated:

| Case | Before | After | Improvement |
|---|---|---|---|
| Open-alert count (dashboard badge), 200k rows, 100,303 matches | Parallel seq scan, 3,499 buffer hits, ~20 ms | Partial-index-only scan, 93 buffer hits, ~10.7 ms | 37× fewer buffer hits, zero heap fetches |
| Fleet cost summary, 120k trip rows | Full per-request aggregation, 2,191 buffer hits, ~40.6 ms | Scheduled materialized view (refresh 156 ms, off-request), 1 buffer hit, ~0.07 ms | ~580× faster on the request path |
| Trip history page at depth 100,000 | OFFSET scan discarding 100k rows, 27,887 buffer hits, ~25.6 ms | Keyset pagination `(start_ts, id)`, 7 buffer hits, ~0.058 ms | ~440× faster, constant cost regardless of depth |
| Vehicle list with alert counts, 200 vehicles | ORM N+1: 201 queries, 1,091 ms | Single `LEFT JOIN + GROUP BY`: 1 query, 36 ms | 201× fewer round trips |

The open-alert case is reported honestly rather than cherry-picked: for the newest-50 queue page itself, the planner already short-circuits after ~78 rows regardless of the partial index, so both plans run at ~0.1 ms — the partial index's real win there is a much smaller footprint (1,040 kB vs. 15 MB), not query latency. It only pays off on the counting/filtering shape, which is why both indexes are kept side by side in migration `0006_perf.sql`.

## 5. Algorithms

A fifteen-item algorithm catalog with complexities and the property tests that back each one lives in `docs/algorithms.md`, implemented under `libs/fscore/algorithms/` and `libs/fscore/sketches/`. It spans VIN/DTC parsing, a safe mapping DSL with no `eval`, an exact anti-replay sliding window for record-level dedup, a Bloom filter used strictly as a cheap negative pre-filter (never a source of truth), Count-Min Sketch plus a min-heap for fleet-wide top-K DTCs, HyperLogLog for distinct-vehicle estimates, watermarked tumbling windows, a hierarchical timing wheel for going-dark detection, geohashing with Union-Find for unapproved depot clustering, A* charger search against a Dijkstra baseline, and PELT-pruned changepoint segmentation against a naive O(n²) baseline.

Two of these were measured directly rather than only complexity-argued (`tools/bench_algorithms.py`):

- **A\* vs. Dijkstra**, 20×20 grid with a blocked central region: both reach the identical optimal cost of 19.000 by expanding the same 300 nodes on an open grid — reported honestly instead of showcasing only a favorable maze. The actual production win is the budget-constrained multi-goal variant used by the low-state-of-charge rule, which prunes out-of-budget chargers up front and finds the answer in 16 expansions instead of 300.
- **PELT vs. naive segmentation**, 1,200-point series with 5 true changepoints: both find the identical five changepoints at identical cost (1358.9), but PELT does it in 35.9 ms against the naive algorithm's 166.3 ms — about 4.6× faster, with the online hysteresis detector staying O(1) per event on the hot path and PELT reserved as the authoritative batch answer.

One deliberate design correction is worth stating on the record: an earlier version used a Bloom filter directly on the per-record dedup path, which can silently drop valid events by construction (a false positive looks identical to a true duplicate). The record path is now an exact anti-replay window (max-sequence plus a 64-bit bitmap, the same technique used in IPsec/DTLS), and the Bloom filter survives only where a false positive is harmless — the gateway's batch-level pre-filter, which is always followed by an exact confirm.

## 6. System design

Ten named system-design principles are mapped to concrete implementation in `spec.md §14.1`, with five Architecture Decision Records and a running decision log in `docs/adr/` and `docs/decisions.md`. The highlights:

- **Effectively-once delivery** via per-partition checkpoint-before-commit plus idempotent sinks, proven by an automated test (`test_checkpoint_before_commit_and_replay_identical_ids`) that replays the log and confirms identical output IDs rather than merely asserting the pattern by design.
- **Back-pressure** through a bounded ingestion queue with an on-disk spill buffer for ordered replay, so a burst degrades gracefully instead of dropping data.
- **Circuit breakers** on downstream calls to prevent cascading failure.
- **CQRS / event sourcing**, with the Kafka-compatible log as the single source of truth and every store (Postgres, Mongo, Redis, the lake) treated as a projection that can be rebuilt from it.

## 7. ML and vector layer

The predictive-maintenance model is documented in `docs/model-card.md`: a LightGBM classifier with isotonic probability calibration, predicting the probability of a breakdown within 7 days from a four-feature vector (`dtc_count`, `harsh_count`, `idle_ratio`, `km_since_service`) — the same vector `RiskScorer` consumes in production. The decision it drives is a cost-optimal inspection list: expected value `p × C_breakdown − C_inspection`, ranked and cut at top-K where expected value is positive.

Training data is explicitly synthetic — 2,000 simulated vehicles, 4,000 rows, generated by a non-leaky wear-state generator where features are strictly computed before the prediction time and labels come from realized breakdowns. The model card states plainly that these numbers prove the pipeline and method, not real-world efficacy, and that retraining on real fleet data is required before any production claim.

Measured results (`ml/artifacts/metrics.json`), compared against a rule baseline (`active DTC → high risk`) and logistic regression, on both a temporal split and a by-VIN split:

| Split | Model | PR-AUC | Precision@50 | Brier | $ saved (K=50) |
|---|---|---|---|---|---|
| Temporal | LightGBM calibrated | 0.120 | 0.14 | 0.056 | $10,000 |
| Temporal | Rule baseline | 0.068 | 0.08 | 0.536 | $2,500 |
| Temporal | Logistic regression | 0.171 | 0.26 | 0.047 | $25,000 |
| By-VIN | LightGBM calibrated | 0.147 | 0.22 | 0.044 | $20,000 |
| By-VIN | Rule baseline | 0.062 | 0.02 | 0.549 | −$5,000 |
| By-VIN | Logistic regression | 0.145 | 0.16 | 0.045 | $12,500 |

The honest reading: the calibrated model decisively beats the rule baseline on both splits — precision@K roughly 2–11× higher, and on the by-VIN split the rule baseline actually loses money ($5,000 lost) while the model saves $20,000. That comparison is the one that matters for deployment, since the rule is what a fleet would run today without this project. Against logistic regression the result is a wash — logreg wins on the temporal split's PR-AUC and dollar savings, the model wins on by-VIN precision@K — which is expected at 4,000 rows, where the underlying signal is close to linear and a gradient-boosted model has little room to pull ahead of a linear one. A three-level noise-robustness sweep and full calibration curves are recorded in `metrics.json` for anyone who wants to check the claim rather than take it on faith.

The vector layer adds two capabilities on top of the four-feature model: 32-dimensional vehicle-day signatures for look-alike k-nearest-neighbour search (so a flagged vehicle can be compared against similar historical cases), and local DTC embeddings for semantic fault-code search. Driver-facing safety scores deliberately do not come from this model — they come from a separate, explainable per-100 km formula with named coaching levers (`ml/src/fs_ml/safety.py`), because a driver-facing or HR-adjacent score needs to be explainable by construction, not just accurate.

## 8. Agentic AI

The Sentinel Copilot is built on LangGraph with a ten-tool allowlist, where tenant scope is injected server-side into every tool call and is never taken from LLM output or conversation text — the same tenant-isolation guarantee the API enforces is never delegated to the model. Every numeric claim the copilot makes is checked against a grounding rule: a number can only appear in the copilot's output if it also appears in a tool result, which is verified automatically rather than trusted. Turns are capped at eight tool calls, with a retry-then-degrade policy if a call fails, and the rules-only briefing path works with no LLM key configured at all, so the system degrades to deterministic output rather than failing closed.

Critically, the copilot's tools are read-only except for one: `propose_action`, which always creates a `pending` action requiring explicit human approval before it has any effect, and every approval carries an idempotency key and is written to the hash-chained audit log. An evaluation harness exercises 5 golden-path scenarios and a 4-item red-team set (tenant-isolation and prompt-injection attempts) against a stub LLM, and the current suite passes green on all of it.

## 9. NFR evidence

| NFR | Target | Evidence |
|---|---|---|
| 100k events/s sustained, 3× burst headroom | Measured peak throughput plus a 2-vs-4-consumer scaling slope, run on Codespace/VM hardware (`tests/load/k6-api.js`), with results extrapolated honestly rather than claimed as a production guarantee |
| Critical alert latency < 5 s | A dedicated priority lane with per-event (not windowed) rules; typically ~1 s, proven by a unit test and shown live via a distributed trace in the demo |
| Ingest-to-screen latency < 2 s | Redis latest-state cache plus WebSocket delta pushes to the console |
| 99.9% availability | Stateless service replicas with PodDisruptionBudgets, plus a chaos test that kills a Kafka consumer mid-burst (`tests/chaos/kill-consumer.sh`) and confirms zero-loss reconciliation afterward |
| Security | STRIDE threat model (`docs/security/stride.md`), Semgrep/Trivy/gitleaks scans in CI, and BOLA (cross-tenant access) tests run against a live Postgres instance, not mocks |

## 10. Testing evidence

The suite currently stands at 403+ green tests spanning unit tests, live-Postgres integration tests, contract tests, BDD scenarios, and the agent evaluation harness, with a CI coverage gate of at least 80% applied to the core packages of every service, not only the algorithm library. Load and chaos test evidence is captured under `docs/evidence/` once the demo VM is running, following the same "measure, don't assert" discipline used for the SQL and algorithm benchmarks above.

## 11. Security and compliance

Security is modeled with STRIDE across six trust boundaries (`docs/security/stride.md`): OEM connector → MQTT, gateway → Kafka, browser → API, agent → LLM provider, the mapping-activation admin path, and the data stores themselves. Each entry in that document states a threat, its control, and where the control is proven — by an automated test, a migration, or an explicitly accepted residual risk. Representative controls: mutual TLS with per-connector client certificates and deny-by-default topic ACLs at the ingestion edge; RS256-validated, JWKS-rotated tokens at the API with tenant identity taken only from the JWT; Postgres row-level security as the hard multi-tenancy backstop (enforced via `SET LOCAL app.tenant_id` per transaction, and proven to hold even against a raw `psql` session, not just application code); pseudonymized driver identifiers and geohash-6 location precision before anything reaches the LLM; and an append-only, hash-chained audit log with a `audit_verify()` function that detects tampering in an integration test by injecting a bad record and confirming the chain reports the exact first bad sequence number.

The project also maps to the brief's own compliance list: OWASP API Top 10 mapping (`spec.md §12.1`), crypto-shredding for right-to-erasure requests (a replay test confirms erased personal data comes back as the literal string `[erased]`, with the key-encryption-key path fail-closed rather than fail-open), k-anonymous aggregate export for privacy-safe data sharing, and a synthetic-data-only rule enforced throughout — the simulator is the only data source in the system, and no real personal or vehicle data exists anywhere in the project.

Accepted residual risks are stated rather than hidden: the local Compose stack runs single-node Postgres/Mongo/Redis (acceptable for a laptop/demo scope, with a Kubernetes high-availability profile specified), a local-dev key wrapper exists in the tree purely to let unit tests run without a live Vault instance (fail-closed and marked `INSECURE`, never used outside `--local`), and Compose Kafka runs without SASL on the assumption of a trusted local network, with SASL/SCRAM specified for the Helm/Terraform path.

## 12. DevOps

The stack runs the same way in every environment it's deployed to. Locally, Docker Compose offers `infra`, `apps`, `obs`, and `ha` profiles. The same services are packaged as a Helm chart for Kubernetes. Terraform provisions a live demo environment (validated, applied for the judging window, and destroyed afterward) alongside a plan-only reference stack for a fuller cloud topology. CI runs a fast-gate pipeline on every push plus a heavier `pr.yml` pipeline covering integration tests, contract tests, BDD scenarios, the web build, container image scanning, and Terraform formatting checks.

The cost model (`docs/cost-model.md`) works from the same arithmetic at every scale, so it is checkable rather than a single asserted number. At 100,000 vehicles sending one event per second, the fleet generates 8.64 billion events/day; at the brief's own raw-JSON baseline (1,000 bytes/event) that would be 8.64 TB/day, but Protobuf plus columnar storage cuts the full-fidelity lake footprint to about 216 GB/day — roughly a 40× reduction — and downsampling to one-minute rollups before cold-archiving it cuts the long-term cold-storage cost by roughly 50× again ($11/month vs. $546/month for seven years of retention). The resulting headline is **~$1,600/month, or about $0.016 per vehicle per month**, at list AWS us-east-1 pricing. The separate live demo VM — one EC2-equivalent instance running k3s with self-hosted Postgres and 500 GB of storage — costs an estimated **$15–25 for the judging window** before it is torn down, with a budget alarm set at $30.

## 13. Demo

The demo video storyboard (`spec.md §21`) is scripted to five minutes across ten beats, each one proving a specific claim rather than narrating generically: the problem framed in numbers; a live WebGL map of the full 100,000-vehicle fleet with real-time KPIs; a tow-away alert firing inside the sub-5-second budget with a full distributed trace; the copilot's daily briefing with grounded numbers, reason codes, human approval, and a verified audit chain; a new OEM onboarded live with zero downtime after a schema-drift detection; a Kafka broker killed mid-burst with a zero-loss reconciliation report; a DuckDB query over a billion-row lake with four before/after `EXPLAIN ANALYZE` comparisons; crypto-shredding erasure proven by replaying the log and seeing `[erased]`; and a closing beat showing green CI, scan results, and the live cloud URL. It is recorded against fixed, rehearsed prompts, and every number shown on screen is read from the live system rather than pre-rendered. The live URL is served from the Terraform-provisioned demo VM, with a Cloudflare Tunnel documented as a free fallback (`docs/demo-url-tunnel.md`) if the VM path is unavailable at submission time.

## 14. Rules and declarations

- **Data:** all data in the system is synthetic, generated entirely by the project's own simulator. The only public datasets referenced are the OpenStreetMap road network (ODbL license, attributed) and generic OBD-II diagnostic trouble code descriptions — neither contains personal or vehicle-owner data.
- **AI and open-source tool usage:** this build used AI coding assistants throughout the 3-day build window, working as parallel agents against contract-first interfaces, with every agent session and reviewer pass logged in `docs/CHAT_LOG.md` as the work happened (not reconstructed afterward). Open-source dependencies are declared in the repository's package manifests (`uv`/`pip` packages in `pyproject.toml`/`uv.lock`), along with the OpenStreetMap ODbL road data and generic OBD-II DTC descriptions used as reference data.
- **Originality:** the problem framing — merging three outputs from one stream, the OEM-cloud connector ingestion model, crypto-shredding inside an event-sourced architecture, and the simulator used as its own correctness oracle — is independently designed and implemented for this project.
- **Affiliation disclaimer (verbatim):** *"Motorq is used only as an industry reference. This is an independent academic exercise with no affiliation to Motorq."*
- **Code freeze:** the submission commit is tagged `v1.0-submission`, with CI green on that tag.

## References

OpenStreetMap road network data (ODbL license); generic OBD-II diagnostic trouble code descriptions; U.S. Department of Energy idling fuel-consumption guidance; AWS us-east-1 list pricing (indicative, re-verified at submission); W3C `traceparent` distributed-tracing specification; the STRIDE threat-modeling framework (Microsoft); the OWASP API Security Top 10 (2023) and OWASP LLM Top 10.
