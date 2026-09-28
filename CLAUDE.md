# CLAUDE.md: rules for every agent working in this repo

Project: **FleetSentinel** (connected-vehicle intelligence platform, hackathon). Design: `spec.md` v3. Work only inside this folder.
**Deadline: Thu 2026-10-01 23:00 IST (unconfirmed).** Read `docs/PROGRESS.md` first: it says what is done, what is blocked, and what is next.

## Living docs (mandatory, kept current in the SAME commit as the work)

| File | Update when |
|---|---|
| `docs/PROGRESS.md` | any stream/milestone status changes, a blocker appears or clears, a plan or date changes. Refresh the "Last updated" line. |
| `docs/BUGS.md` | you find, fix or mitigate any bug or environment issue. Add a row (root cause + how verified). IDs are sequential, never reused. |
| `docs/decisions.md` | you make or change any design, tooling or scope decision that is not already an ADR. If it contradicts `spec.md`, say so in the row. |
| `docs/CHAT_LOG.md` | after every user request or major step: one short dated entry (ask, decision, outcome). |
| `docs/adr/` | only for the 5 formal ADRs (brief asks for 3-5). Do not add more without asking. |

Enforcement: `tools/docs_check.py` runs as a git pre-commit hook (`python tools/install_hooks.py` once per clone) and in CI on PRs.
A change under `libs/ services/ simulator/ ml/ db/ contracts/ deploy/ infra/ tools/ web/ tests/ .github/` MUST also change
`docs/PROGRESS.md` and `docs/CHAT_LOG.md`. `DOCS_CHECK_SKIP=1` exists only for pure-generated changes; justify it in the commit message.
Never edit history to hide a bug: fixed bugs stay in `BUGS.md` with status `FIXED`.

## Frozen contracts (ask before changing)
`contracts/proto/**`, `contracts/topics.yaml`, `contracts/openapi.yaml`, `contracts/mappings/**`, `db/migrations/000{1..7}_*.sql`,
`libs/fscore/src/fscore/{contracts,vin,dtc,hashing,mapping_engine}.py`. If one is wrong, report it (BUGS.md) instead of working around it.
Regenerate Protobuf with `python tools/gen_proto.py` and commit the generated code.

## Working rules
- Stream ownership and definitions of done: `docs/streams/STREAMS.md`. Own only your directories.
- Do not commit or push unless you are the integrator. Never `git push --force`, never rewrite history.
- Deps are pre-declared in each `pyproject.toml`; use `uv run --frozen`. Ask before adding a dependency.
- Tests are part of the work: core/domain/application code >= 80% coverage; `uv run pytest` and `uv run ruff check .` must pass.
- **Memory is scarce (13.9 GB machine, often < 1 GB free).** Never start the whole compose stack; start only the container you need and stop it after. One heavy process at a time.
- Report measured numbers only. If a target is missed, say so.
- Data is synthetic only. No real personal or vehicle-owner data, ever. Secrets never go in git (`deploy/certs/`, `.env` are ignored).
- Windows machine: no `make`. Use `python tools/fs.py <cmd>` (Makefile delegates to it). Shell is Git Bash / PowerShell.
- Motorq is an industry reference only; keep the affiliation disclaimer in README and the Solution Document.
