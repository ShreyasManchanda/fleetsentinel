# CHAT_LOG: living session log

> **Living document.** Append one dated entry per user request or major step: what was asked, what was decided, what changed, and links to the artifacts. Keep entries short. Newest at the bottom.
> Times are IST.

## 2026-09-27

- **Ask:** Read the hackathon brief (`Motorq_Hackathon_Problem_Statement.pdf`), research fully, write `spec.md`. Work only in `C:\Users\Shreyas\Desktop\stephackathon`.
- **Decisions (user):** research and pick the best angle ourselves; 3-day, AI-assisted build; Python-centric backend.
- **Outcome:** merged "FleetSentinel" angle (cost leakage + predictive maintenance + driver safety + copilot). `spec.md` v1 written.
- **Ask:** Two independent reviewers (technical feasibility; completeness against the brief).
- **Outcome:** ~20 findings. Fixed in `spec.md` v2 (Faust dropped, non-leaky ML labels, RLS, back-pressure/circuit-breaker/CQRS naming, Pact, STRIDE scheduling, Rules & Guidelines section).
- **Ask:** "Does it follow the guidelines in the doc?" Outcome: 4 of 5 rules were explicit; added the missing synthetic-data-only rule. Now 5 of 5.

## 2026-09-28

- **Ask:** "Is it good enough to wow judges? Last research run." Outcome: `spec.md` v3 rewrite: 18 contradictions fixed (critical alerts were ~70 s vs the <5 s NFR; Mongo as a raw telemetry store; erasure vs event sourcing; RDS lacks TimescaleDB; etc.) plus 7 hero moments, device-free OEM-connector model, crypto-shredding, simulator-as-oracle, cost model, traceability matrix.
- **Ask:** Deadline is Thursday 11 PM IST (unconfirmed), "run it up". Outcome: **Phase 0** built and committed (`f93d27a`): Protobuf, topics, OpenAPI, 7 migrations, mapping engine, compose/mTLS/Keycloak/Vault, CI skeleton, ADRs, stream briefs. 72 tests pass.
- **Findings during Phase 0:** machine RAM is exhausted (91% committed), Docker Desktop hung until restarted, a 2 GB image pull failed; migrations are syntax-verified only. See `BUGS.md` B-008 to B-013.
- **Ask:** "Make sure there are living md files tracking the chat, implementation plan, bugs and major decisions, and keep them up to date." Outcome: created `docs/PROGRESS.md`, `docs/BUGS.md`, `docs/CHAT_LOG.md` (this file), kept `docs/decisions.md`; added repo `CLAUDE.md` rules, a docs-freshness check (`tools/docs_check.py`, run in CI and as a git pre-commit hook), and a persistent memory rule.
- **Ask (user):** "browser closed, can't close opencode right now, get to work." **Outcome:** memory improved (commit 91% -> 71%, ~1.5 GB free). Docker engine was down again, so its restart runs in the background. Launched **streams A (simulator), B (ingestion + processing), C (API + security)** as parallel background agents on disjoint directories against the frozen contracts (D, E, F queued). Agents do not commit or edit living docs; the integrator records their reports here and in BUGS/decisions. (2026-09-28 16:31 IST)
- **New session (model change, user asked for extra care + a pause point).** Re-read the full transcript, spec.md v3 (1247 lines), STREAMS.md, living docs. Found prior-session agents gone but working tree intact. Verified per package: tests+libs 165, simulator 59, gateway 33 (after fixing B-016: 2 wrong assertions + 1 submit-before-run deadlock, all test-side), normalizer 14, api 23 — **294/294 green**. Noted: `priority_selector` appears only in spec text (§8), correctly absent from contracts (decision-log removal held); spec text needs a touch-up. processor/lake_sink/agent/batch still skeletons, web/ empty, M1 still blocked. Free RAM was down to 0.37 GB. Pausing here per user request; tree committed clean. (2026-09-28 ~19:05 IST)
- **Resume ("continue").** Launched a background builder for processor + lake_sink (stream B remainder; owns only those two dirs). In parallel (non-overlapping): fixed 51 of 61 ruff errors (B-018 partial; libs/ deferred), aligned spec §8.1/§8.2 text with the topic-based priority routing actually implemented (removed stale `priority_selector` references). 144/144 tests green in touched packages; full suite re-check at integration. (2026-09-28 23:19 IST)
