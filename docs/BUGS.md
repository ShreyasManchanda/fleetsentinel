# BUGS: living defect and issue log

> **Living document.** Add a row the moment a bug or environment issue is found; update its status when fixed. Include root cause and how it was verified.
> Statuses: `OPEN`, `FIXED`, `MITIGATED`, `WONTFIX`. IDs are sequential and never reused.

| ID | Date | Area | Summary | Root cause | Status | Fix / verification |
|---|---|---|---|---|---|---|
| B-001 | 2026-09-28 | contracts/proto | `protoc` failed: enum values `TOW`/`CRASH` defined twice | proto3 enum values share the package namespace, so `EventType.TOW` collided with `RuleCode.TOW` | FIXED | Prefixed all `RuleCode` values with `RULE_`; regenerated; smoke test round-trips |
| B-002 | 2026-09-28 | fscore.vin | `make_vin` produced 18-char VINs, so `zip(strict=True)` raised | Body list had 14 random chars instead of 13 | FIXED | Corrected to 13; Hypothesis tests assert every generated VIN validates |
| B-003 | 2026-09-28 | contracts/mappings samples | OEM_B sample VIN failed validation | Hand-written VIN had an invalid check digit (the validator was right) | FIXED | Replaced with a generated valid VIN; mapping test passes |
| B-004 | 2026-09-28 | fscore tests | Wrong odometer expectation in OEM_B test | Arithmetic error: 11330.2 mi = 18234.2 km, not 18235.9 | FIXED | Corrected the expectation |
| B-005 | 2026-09-28 | db/migrations 0004 | `audit_verify` would only see the caller's tenant rows under RLS, breaking the chain check | RLS applies to `fs_app`; the chain is global | FIXED | Function is `SECURITY DEFINER` with pinned `search_path` (syntax-verified; runtime check pending M1) |
| B-006 | 2026-09-28 | db/migrations 0003 | RLS and Timescale compression do not combine on all versions | Timescale limitation | MITIGATED | Tenant access to hypertable/continuous aggregates goes through security-barrier views; base tables revoked from `fs_app` (runtime check pending M1) |
| B-007 | 2026-09-28 | ruff config | 32 lint errors (E501 long lines, S603 subprocess) | Default rules too strict for docs/dev tooling | FIXED | Ignore E501; per-file-ignore S603/S607 in `tools/**`; `ruff check` clean |
| B-008 | 2026-09-28 | env / Docker | Docker Desktop engine returned 500 for 25+ min | Backend waited on a hung WSL init control API | FIXED | Killed Docker processes, `wsl --shutdown`, relaunched; engine up in ~10 s |
| B-009 | 2026-09-28 | env / Docker | `docker pull timescale/timescaledb-ha:pg17` died with `unexpected EOF` after 13 min | Network drop on a ~2 GB pull | OPEN | Retry with free RAM; fallback: validate on the cloud VM |
| B-010 | 2026-09-28 | env / RAM | bash `fork: Resource temporarily unavailable`; docker CLI `out of memory` | Committed memory 91% (54.7 of 60 GB), 230 MB physically free; browser ~5 GB, opencode ~2 GB, Traycer ~2 GB | OPEN | Needs the user to free memory; limits parallel agents to 2-3 |
| B-011 | 2026-09-28 | fscore.mapping_engine | Throughput ~11k records/s/core | Per-record Python closure calls; 13 fields x transforms | OPEN | Assigned to stream B: batch-vectorised fast path, target >= 40k rec/s/core; guard test asserts > 8k |
| B-012 | 2026-09-28 | repo / git | Home directory `C:\Users\Shreyas` is itself a git repo, so `git status` in the project showed the wrong root | Pre-existing parent repo | MITIGATED | `git init` inside the project; verified `rev-parse --show-toplevel` is the project |
| B-013 | 2026-09-28 | db/migrations | Migrations have never run against a real Postgres | Image pull + RAM blocked (B-009, B-010) | OPEN | Run `python tools/fs.py migrate` as soon as M1 is unblocked; log any semantic errors here |
| B-014 | 2026-09-28 | spec v2 | 18 contradictions found in the final research pass (Mongo raw telemetry, 70 s critical alerts, erasure vs event sourcing, RDS without Timescale, etc.) | Spec-level design flaws | FIXED | Fixed in spec.md v3; list in spec section 25 |
