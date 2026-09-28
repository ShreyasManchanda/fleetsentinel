# PROGRESS: living implementation plan and status

> **Living document.** Update it in the same change as the work it describes (rules in `CLAUDE.md`). Newest status first inside each section.
> Last updated: **2026-09-28 16:05 IST** by the integrator.

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
| M1 | Migrations verified on a real Timescale/pgvector Postgres | Mon 28 Sep night / Tue AM | BLOCKED (image pull + RAM, see Blockers) |
| M2 | Thin slice: simulator -> gateway -> Kafka -> normalizer -> processor -> Redis -> live vehicles visible | Tue 29 Sep EOD | NOT STARTED |
| M3 | Alerts + API + auth + basic copilot real | Wed 30 Sep midday | NOT STARTED |
| M4 | Feature freeze (all T0 + agreed T1) | Wed 30 Sep 22:00 | NOT STARTED |
| M5 | Evidence: load/burst/soak/chaos on cloud VM, reconciliation, 4 EXPLAIN cases, evals | Thu 1 Oct AM | NOT STARTED |
| M6 | Solution Document, demo video (<=5 min), README, tag `v1.0-submission` | Thu 1 Oct by 18:00 | NOT STARTED |

## Stream status (details: `docs/streams/STREAMS.md`)

| Stream | Scope | Owner | Status | Notes |
|---|---|---|---|---|
| Phase 0 | contracts, migrations, mapping engine, infra config | integrator | DONE | 72 tests, 100% on vin/dtc/hashing/contracts, 91% mapping engine |
| A | simulator + seed | unassigned | NOT STARTED | needs OSM route artifact early |
| B | ingestion + stream processing | unassigned | NOT STARTED | owns the vectorised mapping fast path (>= 40k rec/s/core) |
| C | API + security | unassigned | NOT STARTED | RLS/BOLA + crypto-shredding tests are the gates |
| D | intelligence (ML + copilot + batch) | unassigned | NOT STARTED | needs stream A ground truth |
| E | web | unassigned | NOT STARTED | can start on a mock of `openapi.yaml` |
| F | DevOps + QA + docs | unassigned | NOT STARTED | Terraform live stack is on the critical path for evidence |

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
| Local RAM: ~91% committed, ~230 MB free; browser + `opencode` + Traycer dominate | Docker stack and parallel agents will thrash or crash | OPEN, needs user | Ask user to close browser tabs / opencode; run 2-3 agents max; heavy evidence on cloud VM |
| `timescale/timescaledb-ha:pg17` (~2 GB) pull dies with `unexpected EOF` | Cannot test migrations locally | OPEN | Retry when memory is free; fallback = test on cloud VM |
| Mapping engine ~11k rec/s/core | 100k/s needs ~9 cores of normalization | OPEN (assigned to stream B) | Batch-vectorised fast path, report measured rate |
| Solution Document template not in the folder | Submission format risk | OPEN, needs user | User to supply; content is pre-structured in spec section 20 |
| Deadline unconfirmed | Schedule | OPEN, needs user | Confirm date/time and timezone |
| Docker Desktop hung at first start | Lost ~25 min | RESOLVED | Killed processes + `wsl --shutdown` + restart |

## Next actions (ordered)

1. ~~Land living docs + `CLAUDE.md` + docs check; commit.~~ Done.
2. User frees memory; retry the Postgres image pull; run `fs migrate` for real (M1).
3. Launch streams A, B, C first (two or three agents at a time), then D, E, F.
4. Daily: update this file, `BUGS.md`, `decisions.md`, `CHAT_LOG.md`; commit.

## Change log of this plan

- 2026-09-28: created. Plan derived from spec.md v3 section 24; dates mapped to the Thu 1 Oct 23:00 IST deadline.
