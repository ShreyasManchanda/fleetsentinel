# FleetSentinel demo video — recording runbook

> **Updated 2026-10-01 (evening).** Read "Pre-flight" first. The live stack reaches alerts end to end, but the console's KPI tiles, Copilot briefing and driver scorecards are empty live (BUGS B-032), so **UI beats are recorded in mock mode and narrated as fixture data**; terminal beats run live.

## Pre-flight (do this before pressing record)

Mock-mode console (UI beats 2, 4, 5, 8, 9): in `web/`, `.env` must contain only `VITE_HIDE_MOCK_BANNER=1` (no `VITE_API_BASE_URL`, no `VITE_OIDC_ISSUER`), then `npm.cmd run dev` and open http://localhost:5173. Log in with **Continue with mock token** *before* recording starts (the login screen carries a DEV ONLY label), so the recording opens on the Command Center. Hiding the banner only removes the banner; when you speak, say "recorded fixture data" for anything read from the mock.

Live stack (terminal beats 3, 5-trigger, 6, 7), all from the repo root, one terminal each:
1. Infra is already up if `docker ps` lists 8 healthy `fleetsentinel-*` containers; otherwise `python tools/fs.py bootstrap`.
2. API: `FS_API_KAFKA_BOOTSTRAP=localhost:19092 uv run --frozen python -m fs_api` (port 8000).
3. Agent: `uv run --frozen uvicorn fs_agent.service:app --port 8100`.
4. Gateway: `uv run --frozen python -m fs_gateway.main`.
5. Normalizers: `FS_NORMALIZER_LANE=bulk FS_NORMALIZER_METRICS_PORT=9103 uv run --frozen python -m fs_normalizer.main` and the same with `LANE=priority`, port `9104`.
6. Processors: `FS_LANE=priority uv run --frozen python -m fs_processor.main` and `FS_LANE=bulk ...`.
7. State bridge: `uv run --frozen python tools/redis_state_bridge.py` (B-031 workaround).
8. Simulator (must be the full 100k fleet; a smaller `--vehicles` makes VINs the database does not know): `uv run --frozen python -m fs_simulator.publish --duration 1800 --certs deploy/certs`.
9. To show the live web console instead of mock: put `VITE_API_BASE_URL=http://localhost:8000`, `VITE_OIDC_ISSUER=http://localhost:8080/realms/fleetsentinel`, `VITE_OIDC_CLIENT_ID=fs-web` in `web/.env`, log in with Keycloak as `admin@apex-logistics.demo` / `Passw0rd!`. Live, only the alert feed and map are populated.

Honest-narration rule: never read a number the screen did not show, and never call mock data live.

For a presenter who has never used the app. Follow it word-for-word. Total run time budget: **4:45**, leaving a **:15 buffer** against the 5:00 hard cap (spec.md §21).

**Mock-mode note:** every beat below has a mock-mode fallback because `web/` is mock-first (`web/README.md`): with no `VITE_API_BASE_URL` set, `npm.cmd run dev` serves every screen from the typed mock in `web/src/api/mock.ts`, which implements the full `contracts/openapi.yaml` shape. Numbers quoted in SAY below are the ones the live system is designed to show; if recording against mock mode instead of the live stack, re-read whatever the mock actually renders in that moment and say that number instead of the scripted one — never read a scripted number the screen doesn't show.

## If something breaks live — recovery box

1. **Stop talking about the failure.** Don't narrate the bug — pause, breathe, keep the sentence you were mid-way through generic ("…and the system flags this in real time").
2. **Switch the browser tab** to the pre-opened mock-mode tab (`npm.cmd run dev` with no `VITE_API_BASE_URL`, already loaded at `http://localhost:5173` before recording starts).
3. **Say:** "Here's the same view on our recorded fixture data" — then continue the SAY script for that beat; mock mode renders the same screens and the same shapes.
4. **Never re-run a failed command on camera.** If a trigger script errors, cut to the next beat's screen and fold the missed proof point into the closing beat's CI/evidence summary instead.
5. **Keep the clock in view** (phone timer, off-camera) — if a beat overruns by more than 10s, skip its DO entirely and jump straight to its CHECK screen already pre-loaded in another tab.

---

## Two cuts from one recording session

The official brief (`Motorq_Hackathon_Problem_Statement.pdf` §13, `spec.md` §21) asks for a video of **5 minutes or less**. So record once, and cut two versions:

| Cut | Length | Use |
|---|---|---|
| **Submission cut** | **<= 4:50** | What you upload. Beats marked **S**. |
| **Extended walkthrough** | **~8:40** | Optional: a longer demo for judges who ask, a portfolio, or a live presentation. All beats. |

If the submission form enforces the 5-minute rule, upload the submission cut. Do not upload the extended cut as the submission.

## What the screens actually show (verified in a dry run, mock mode)

Quote these, not the numbers in older drafts. If you see something different on the day, say what you see.

- **Command Center:** Open critical alerts **15**, $ leaking today **$18,400**, At-risk vehicles **30,544 (30.5%)**, Savings opportunity / week **$52,300**, Freshness "~7s old", "ingest p95 1240 ms". Alert feed rows are status text only (not clickable, no Ack button; the only button is "Refresh feed"). The map card reads "MapLibre / Viewport points (zoom 10.0)".
- **Copilot:** briefing says "5 actions could save **$18,400** this week". The chat reply to "Why is this VIN flagged?" is a fleet summary ("20 open critical alerts and $18,400 leaking today ... depot idling at the Peenya cluster"), not reason codes. **Reason codes are on Vehicle 360.** Approve on the top queue row (a `coach_driver` proposal, $3,071) removes it from Pending.
- **Platform:** order is Mapping editor, Mapping versions, Data quality, then the dead-letter queue. The button is **Run preview**. There is **no approve/activate or replay button**, so narrate preview only. Preview also prints a mock "unknown_field" failure line; do not dwell on it.
- **Compliance:** Verify chain returns "Chain OK - 200 entries". ERASE with any UUID shows "accepted - status received". Load preview shows "k=10 ... 12 aggregate rows". There is no `[erased]` replay screen in the UI.
- **Vehicle 360:** open VIN **MLHD38JWYJFPY1CUT** (86.0% p(failure, 7d), Recommendation INSPECT, Expected value $3,432, reasons dtc_rate_7d, idle_share_30d, soc_min_7d). Alert-feed VIN suffixes do not open the right vehicle.
- **Drivers:** open Driver 004 (score 50; harsh brake 1.8; idle share 0.14; "2.0x peer median - coach anticipation braking", "Idling 75% above peers").

## Beat-by-beat

Times are submission cut (S) / extended cut (E). The presenter reads SAY; DO is what you click.

### Beat 1 - The problem in numbers (S 0:00-0:20 / E 0:00-0:45) **S**
**ON SCREEN:** a title slide or architecture diagram (from `docs/solution-document-final.md` or spec.md), no app yet.
**SAY:** "FleetSentinel is a connected-vehicle intelligence platform: a hundred thousand mixed ICE and EV vehicles, one normalized telemetry pipeline, three questions: where are we losing money, what breaks next, and who is driving riskily. A guarded AI copilot turns the answers into approved, audited actions. Everything you see is synthetic data."
**E adds:** "At one event a second that is 8.64 billion events a day; stored as Protobuf and Parquet it is roughly a fortieth of the raw JSON footprint."

### Beat 2 - Command Center (S 0:20-1:00 / E 0:45-2:00) **S**
**DO:** open `/` (already logged in). Sweep the cursor across the five tiles, then the alert feed.
**SAY:** "The Command Center: open critical alerts, dollars leaking today from idling, vehicles at elevated risk, and the savings opportunity for the week. The freshness tile shows how old the data is. [If recording from mock, say: this is recorded fixture data.] Critical alerts run on their own lane, designed to land in under five seconds."
**E adds:** read the four tile numbers aloud, point at the map card ("the WebGL map over Bengaluru"), click **Refresh feed**.
**Honest note:** do not claim "moving dots"; the map card shows a point count.

### Beat 3 - Live pipeline proof, terminal (S 1:00-1:35 / E 2:00-3:30) **S**
The one beat that can be genuinely live. Use the pre-flight stack.
**DO:** show the simulator terminal publishing, then `docker ps` (eight healthy containers), then processor log lines `priority_batch ... alerts: N`; optionally `docker exec fleetsentinel-postgres-1 psql -U postgres -d fleetsentinel -c "select count(*) from alert"` run twice to show it growing.
**SAY:** "This is the real pipeline: simulated OEM connectors publish over mutual-TLS MQTT, the gateway deduplicates into Kafka, the normalizer maps four OEM formats to one schema, and the priority processor evaluates rules per event and writes alerts to Postgres with row-level security per tenant."
**E adds:** switch `web/.env` to live per pre-flight and show the alert feed updating; show normalizer batch logs.
**Do not claim** a measured end-to-end sub-5-second trace or a tow-specific trigger unless you measured it (see Flagged gaps).

### Beat 4 - Copilot (S 1:35-2:30 / E 3:30-5:00) **S**
**DO:** `/copilot`. Point at the Daily briefing. Type "Why is this VIN flagged?" and send. Approve the top row in the Approval queue.
**SAY:** "The morning briefing: five actions worth $18,400 this week. The copilot answers from tool results, and it can only propose actions; nothing executes until a human approves. I approve this coaching action and it leaves the pending queue."
**E adds:** Vehicle 360 for `MLHD38JWYJFPY1CUT`: "here are the reason codes behind a flag: dtc rate, idle share, minimum state of charge; 86% failure probability in seven days, recommendation inspect." Then Drivers, Driver 004: "peer-relative scorecards with the levers that move the score."
**Honest note:** do not say the approval appears in the audit log; the mock does not do that.

### Beat 5 - OEM onboarding and schema drift (S 2:30-3:10 / E 5:00-6:15) **S**
**DO:** `/platform`. Show the dead-letter queue sample (OEM_D rows), then the Mapping editor and **Run preview**, then Data quality per OEM (OEM_D flagged "drift").
**SAY:** "When an OEM changes its payload we do not crash or silently drop data: records land in the dead-letter queue, drift is detected per OEM, and a new mapping is previewed against sample payloads before it goes live. Mappings are data, not code, so onboarding needs no redeploy."
**Honest note:** the UI has no approve or replay button. Say "preview", not "approve and replay".
**E adds:** terminal `uv run --frozen python -m fs_simulator.publish --force-drift`, then the normalizer log showing OEM_D failures.

### Beat 6 - Resilience and scale (S 3:10-3:40 / E 6:15-7:15)
Show only what you have measured. **S:** one spoken sentence over the architecture diagram: "Priority and bulk lanes are separate consumer groups, so a bulk backlog cannot delay critical alerts; the Helm chart and Terraform reproduce the stack in the cloud."
**E adds:** `tests/chaos/kill-consumer.sh` output **only if you ran it**. It kills a consumer, not a broker, so say "consumer".

### Beat 7 - Data lake and SQL (E 7:15-7:45 only)
Only with real numbers from `docs/evidence/`. Otherwise skip. Do not say "billion rows" unless the lake has them.

### Beat 8 - Privacy: erasure and k-anonymity (S 3:40-4:15 / E 7:45-8:10) **S**
**DO:** `/compliance`. Type any UUID, click **ERASE**, **Submit DSR**. Then **Load preview** on Privacy export.
**SAY:** "A right-to-erasure request. Driver identifiers are encrypted per driver, so erasure destroys the key. Exports for analytics are k-anonymous: small cells are suppressed, with no VINs or driver identifiers."

### Beat 9 - Audit chain and close (S 4:15-4:50 / E 8:10-8:40) **S**
**DO:** click **Verify chain** ("Chain OK - 200 entries"). Then cut to the repo/CI slide.
**SAY:** "Every action is in a hash-chained audit log, verified with one click. Behind this: a STRIDE threat model, tenant isolation tested against a real Postgres, and 671 tests at 90% coverage. Motorq is an industry reference only; this is an independent academic exercise. Thanks for watching."
**Do not** show a live cloud URL unless one is actually deployed.

## Timing

| Cut | Beats | Total |
|---|---|---|
| Submission (S) | 1, 2, 3, 4, 5, 6 (one sentence), 8, 9 | 4:50, keep a 10 s margin under 5:00 |
| Extended (E) | 1 to 9 | about 8:40 |

## Flagged gaps (could not confirm against current code in this pass)

- **Beat 3 (tow trigger):** no dedicated CLI/API trigger for a tow-specific scenario was found in `simulator/src/fs_simulator/publish.py` — only generic drift/force-drift controls. Use a pre-seeded ground-truth incident and a known VIN instead of a live trigger, or confirm with whoever owns the simulator that such a trigger exists.
- **Beat 6 ("kill a broker"):** `tests/chaos/kill-consumer.sh` name suggests it kills a consumer, not a Kafka broker. Verify the actual failure mode before claiming "broker kill" on camera.
- **Beat 7 (1B-row DuckDB query + 4 EXPLAIN ANALYZE pairs):** script location and actual row count not confirmed in this pass. `simulator/src/fs_simulator/seed.py --profile full` is the seed profile most likely to build the large lake, but the exact row count and the EXPLAIN ANALYZE evidence files need to be located and their real numbers substituted before recording — do not say "1 billion" unless verified.
- **Readiness (resolved 2026-10-01):** `docs/PROGRESS.md`, `docs/BUGS.md` (B-030..B-032) and `docs/solution-document-final.md` §13 now agree: ingestion and alerts are live, the KPI/briefing/driver screens are not (use mock mode for them), the cloud URL and load/chaos evidence runs are unconfirmed. Do not claim anything the pre-flight did not show.
