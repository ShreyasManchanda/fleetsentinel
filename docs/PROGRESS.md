# PROGRESS: living implementation plan and status

> **Living document.** Update it in the same change as the work it describes (rules in `CLAUDE.md`). Newest status first inside each section.
> Last updated: **2026-10-01 (submission day, evening)** by the integrator: live stack run end to end (alerts reach the UI), processor wiring fixed (B-030), Redis bridge added (B-031); KPI/briefing/driver screens still need batch jobs (B-032). Demo plan: mock-mode UI + live terminal beats.

## Clock

| Item | Value |
|---|---|
| Deadline (unconfirmed, please verify) | **Thu 2026-10-01 23:00 IST** |
| Now | Mon 2026-09-28 (Phase 0 done) |
| Hours left at last update | ~79 h |
| Freeze target (feature freeze, evidence only after) | Wed 2026-09-30 ~22:00 IST |
| Submission target (buffer for upload / template) | Thu 2026-10-01 ~18:00 IST, tag `v1.0-submission` |

## Milestones

| # | Milestone | Target | Status |
|---|---|---|---|
| M0 | Phase 0: contracts + skeleton committed | Mon 28 Sep | DONE (commit `f93d27a`) |
| M0b | Living docs + repo `CLAUDE.md` + docs check | Mon 28 Sep | DONE (pre-commit hook + CI `docs-fresh` job) |
| M1 | Migrations verified on a real Timescale/pgvector Postgres | Mon 28 Sep night / Tue AM | DONE (2026-09-29 ~00:30 IST: `fs-pg` up, all 7 applied clean; timescaledb 2.30.1/vector 0.8.6; RLS proven B-019; B-013 FIXED). Container left running for stream C/D use |
| M2 | Thin slice: simulator -> gateway -> Kafka -> normalizer -> processor -> Redis -> live vehicles visible | Tue 29 Sep EOD | PARTIAL (broker-free path DONE: `tests/integration/test_thin_slice.py` — OEM payload bytes → normalize → priority TOW alert + bulk states + lake Parquet object, 2 tests green. Live-broker run needs Redpanda+Mosquitto+Redis; RAM-gated) |
| M3 | Alerts + API + auth + basic copilot real | Wed 30 Sep midday | NOT STARTED |
| M4 | Feature freeze (all T0 + agreed T1) | Wed 30 Sep 22:00 | NOT STARTED |
| M5 | Evidence: load/burst/soak/chaos on cloud VM, reconciliation, 4 EXPLAIN cases, evals | Thu 1 Oct AM | PARTIAL (4 EXPLAIN cases, 665-test run at 90% coverage and live-Postgres security tests saved in docs/evidence; load/soak/chaos/scans NOT run) |
| M6 | Solution Document, demo video (<=5 min), README, tag `v1.0-submission` | Thu 1 Oct by 18:00 | IN PROGRESS (solution doc final written; demo runbook updated for mock-mode UI + live terminal beats; recording not done) |

## Stream status (details: `docs/streams/STREAMS.md`)

| Stream | Scope | Owner | Status | Notes |
|---|---|---|---|---|
| Phase 0 | contracts, migrations, mapping engine, infra config | integrator | DONE | 72 tests, 100% on vin/dtc/hashing/contracts, 91% mapping engine |
| A | simulator + seed | agent (lost, see log) | PARTIAL | 33 py files, 59 tests pass; no final report (agent rate-limited). Needs verification vs §6/DoD |
| B | ingestion + stream processing | dead bg builder (draft) + integrator tests | NEARLY DONE | gateway (33) + normalizer (14) + libs (150+) green. Processor domain+application now covered: 30 new tests (rules, lifecycle, scoring, priority incl. event-time GOING_DARK, bulk incl. checkpoint-restore replay determinism). Lake sink: 6 tests (naming, Parquet, idempotent rewrite). Caught real bug B-023 (BulkRunner offset). Remaining: Kafka/Redis adapter integration (needs broker) |
| C | API + security | agent (lost, see log) | PARTIAL | 67 py files, 23 tests pass; no final report. RLS/BOLA gates need real-Postgres check (M1) |
| D | intelligence (ML + copilot + batch) | integrator (builder died B-025) | DONE | ml/ (37 tests, model+card) + agent (9 tests: tools/guardrails/briefing/LangGraph/stub-evals, FastAPI service) + batch (5 tests: DuckDB features/recompute/reports/reconcile, CLI). 395 total green |
| E | web | agent (bg task, done) + integrator verified | DONE | 6 screens mock-first vs openapi.yaml, Command Center + deck.gl map; verified: typecheck/lint clean, 15 vitest green, build 7s. Real-API e2e + WebSocket pending stream C |
| F | DevOps + QA | integrator (2 dead builders) | NEARLY DONE | cost model, Helm chart (services+PDB+NetPol+HPA+CronJob+ingress), Terraform live (VALIDATED) + reference (VALIDATED), k6 script, chaos script, pr.yml CI, contract tests (3), BDD (5 scenarios). Left: apply/destroy on cloud VM, load/chaos runs, ZAP scan |

## Phase 0 checklist (done)

- [x] Protobuf events (telemetry, alert, state, audit) + generated Python committed
- [x] Topic registry (`contracts/topics.yaml`) incl. priority lane topic
- [x] OpenAPI 3.1 contract (35 paths, 43 schemas) validated in CI
- [x] 7 SQL migrations (3NF, RLS, Timescale, audit chain, reference data, perf, grants), 104 statements syntax-verified
- [x] OEM mapping DSL + engine + 4 mappings + samples (OEM_D is the live-onboarding demo)
- [x] Compose stack, Mosquitto mTLS + ACLs, cert generator, Vault init, Keycloak realm generator
- [x] `tools/fs.py` CLI, Makefile, CI skeleton (unit, schema-compat, security-fast), Dockerfile
- [x] 5 ADRs, decision log, stream briefs, README with affiliation disclaimer
- [x] Living docs (`PROGRESS`, `BUGS`, `CHAT_LOG`, `decisions`) + `CLAUDE.md` + `tools/docs_check.py` gate (hook + CI); 77 tests pass
- [ ] Migrations executed against real Postgres (blocked)

## Blockers and risks (owner, status)

| Blocker / risk | Impact | Status | Next action |
|---|---|---|---|
| Local RAM: browser closed -> 71% committed, ~1.5 GB free (was 91% / 230 MB); `opencode` (~2 GB) + Traycer still resident | Docker stack + agents may still thrash | WORSE AGAIN | 2026-09-29 ~00:05 IST: back to **92.8% committed, ~1 GB free**. Docker engine down again (pipe missing). No container work until memory frees; builders work with fakes/--local only |
| `timescale/timescaledb-ha:pg17` (~2 GB) pull dies with `unexpected EOF` | Cannot test migrations locally | FIXED | 2026-09-29: layers were cached (4.05 GB image present); pull completed instantly after engine restart |
| Mapping engine ~11k rec/s/core | 100k/s needs ~9 cores of normalization | OPEN (assigned to stream B) | Batch-vectorised fast path, report measured rate |
| Solution Document template not in the folder | Submission format risk | OPEN, needs user | User to supply; content is pre-structured in spec section 20 |
| Deadline unconfirmed | Schedule | OPEN, needs user | Confirm date/time and timezone |
| Docker Desktop hung at first start | Lost ~25 min | RESOLVED once; engine stopped answering again later | Restart via script; engine start is being retried in the background |

## Next actions (ordered)

1. ~~Land living docs + `CLAUDE.md` + docs check; commit.~~ Done.
2. Get Docker engine up, pull `timescale/timescaledb-ha:pg17`, run `fs migrate` for real (M1). Not blocking streams A-C (they use fakes / --local).
3. ~~Launch streams A, B, C~~ launched Mon 2026-09-28 afternoon. Launch D and E when two of them land; F (DevOps) last or alongside.
4. Integrate returned work: run full test suite, ruff, docs_check; record agent-reported bugs/decisions; commit.
5. Daily: update this file, `BUGS.md`, `decisions.md`, `CHAT_LOG.md`; commit.

## Change log of this plan

- 2026-09-28: created. Plan derived from spec.md v3 section 24; dates mapped to the Thu 1 Oct 23:00 IST deadline.
- 2026-09-28: streams A, B, C launched as background agents (3 at a time because of RAM). D, E, F queued.
- 2026-09-28 19:05 IST: new session (model change). Prior-session agents are gone with no final reports, but their working-tree output survived. Integrator verified: **all 294 tests pass** (tests+libs 165, simulator 59, gateway 33, normalizer 14, api 23) after fixing 3 test-side bugs in gateway (B-016). processor/lake_sink/agent/batch still skeletons, web/ empty. Next: commit this state, then finish processor + lake_sink (stream B remainder) before D/E/F.


