# FleetSentinel demo video — recording runbook

For a presenter who has never used the app. Follow it word-for-word. Total run time budget: **4:45**, leaving a **:15 buffer** against the 5:00 hard cap (spec.md §21).

**Mock-mode note:** every beat below has a mock-mode fallback because `web/` is mock-first (`web/README.md`): with no `VITE_API_BASE_URL` set, `npm.cmd run dev` serves every screen from the typed mock in `web/src/api/mock.ts`, which implements the full `contracts/openapi.yaml` shape. Numbers quoted in SAY below are the ones the live system is designed to show; if recording against mock mode instead of the live stack, re-read whatever the mock actually renders in that moment and say that number instead of the scripted one — never read a scripted number the screen doesn't show.

## If something breaks live — recovery box

1. **Stop talking about the failure.** Don't narrate the bug — pause, breathe, keep the sentence you were mid-way through generic ("…and the system flags this in real time").
2. **Switch the browser tab** to the pre-opened mock-mode tab (`npm.cmd run dev` with no `VITE_API_BASE_URL`, already loaded at `http://localhost:5173` before recording starts).
3. **Say:** "Here's the same view on our recorded fixture data" — then continue the SAY script for that beat; mock mode renders the same screens and the same shapes.
4. **Never re-run a failed command on camera.** If a trigger script errors, cut to the next beat's screen and fold the missed proof point into the closing beat's CI/evidence summary instead.
5. **Keep the clock in view** (phone timer, off-camera) — if a beat overruns by more than 10s, skip its DO entirely and jump straight to its CHECK screen already pre-loaded in another tab.

---

## Beat-by-beat

### Beat 1 — The problem in numbers (0:00–0:18, 18s)
**GOAL:** Frame scale and the promise ("trustworthy in seconds") before showing any UI.
**ON SCREEN:** Title card / architecture diagram (from solution-document or spec.md), not the app yet.
**SAY:** "FleetSentinel ingests telemetry from 100,000 connected vehicles — about 8.6 terabytes of raw data a day — and turns it into decisions that are trustworthy within seconds, not hours."
**DO:** Show a static slide with "100,000 vehicles" / "8.6 TB/day" / "<5s critical alerts" — no clicks.
**CHECK:** Slide is legible, no app chrome visible yet.

### Beat 2 — Command Center: live map + KPIs (0:18–0:55, 37s)
**GOAL:** Prove real-time scale and the UI.
**ON SCREEN:** Command Center (`web/src/pages/CommandCenter.tsx`) — "Live vehicle map — Bengaluru" WebGL card, and the 5-tile KPI strip: "Open critical alerts", "$ leaking today", "At-risk vehicles", "Savings opportunity / week", "Freshness".
**SAY:** "This is the Command Center: 100,000 vehicles live on Bengaluru roads. The KPI strip on top shows dollars leaking right now, savings opportunity for the week, and data freshness — this is p95 ingest-to-screen latency, not a static dashboard."
**DO:** Navigate to `http://localhost:5173/` (Command Center is the default route). Let the map render fully before speaking. Point the cursor at the "Freshness" tile, then sweep across the KPI row left to right.
**CHECK:** Map shows moving dots on real road geometry; all 5 KPI tiles show numbers, not `Loading…` or `Empty` states.

### Beat 3 — Trigger a tow alert (0:55–1:28, 33s)
**GOAL:** Prove the critical-alert lane is under 5 seconds end-to-end, with a trace.
**ON SCREEN:** Command Center "Live alert feed" card; then a Tempo/Jaeger trace view (or the equivalent trace screenshot in `docs/evidence/`) ingest→screen.
**SAY:** "Now I'll trigger a tow scenario on one vehicle. Watch the alert feed — the critical lane processes this as a single event, not a batch window, so it lands in about one to two seconds." *(after it appears)* "And here's the trace: ingest, gateway, Kafka, processor, to this screen."
**DO:** Run the simulator's fault trigger (`python -m fs_simulator.publish --dry-run --force-drift` is for OEM_D drift, not tow — if the stack exposes a tow-fault trigger script/endpoint, run that script here; otherwise use the seeded fixture: `python -m fs_simulator.seed --profile lite` already plants a tow incident in ground truth, so point at a known-flagged VIN in the alert feed instead of live-triggering one). Click the alert row that appears, then switch to the trace viewer tab.
**CHECK:** Alert appears in the feed within the stated window; trace shows a continuous span from the ingest service through to the API.
**NOTE:** No dedicated "trigger tow scenario" CLI flag was found in `simulator/src/fs_simulator/publish.py` (only `--force-drift` for OEM_D and `--control-file` for drift control) — flag this gap to whoever owns the simulator before recording; the beat may need to use a pre-seeded incident instead of a live trigger.

### Beat 4 — Copilot: briefing, explain, approve, audit (1:28–2:12, 44s)
**GOAL:** Prove the ML decision, agent guardrails, and the audit trail together.
**ON SCREEN:** Copilot (`web/src/pages/Copilot.tsx`) — "Daily briefing" card, then the chat panel, then "Approval queue".
**SAY:** "Each morning the copilot briefs the fleet manager: five actions worth acting on this week, worth about twenty thousand dollars in avoided breakdowns. I'll ask why one vehicle is flagged." *(after chat reply)* "It gives reason codes, not a black box. I'll approve the recommended action — that creates a work order, and every step is written to a hash-chained audit log."
**DO:** Go to the Copilot screen. Point at the "Daily briefing" card first. Type into the chat input: "Why is this VIN flagged?" and submit (Button disabled until text entered). Scroll to "Approval queue", click **Approve** on the top row.
**CHECK:** Briefing card shows a non-empty action count and a dollar figure (not `Loading…`); chat response includes reason codes / DTC explanation; approved row moves out of the "pending" filter.

### Beat 5 — Live OEM onboarding, zero downtime (2:12–2:52, 40s)
**GOAL:** Prove multi-OEM normalization and no-redeploy onboarding.
**ON SCREEN:** Platform (`web/src/pages/Platform.tsx`) — "Dead-letter queue (sample)" card, then "Mapping editor + live preview", then "Data quality per OEM".
**SAY:** "OEM_D just started streaming a format we've never seen. Instead of crashing or dropping data, it lands in the dead-letter queue while the copilot drafts a mapping for it. I preview the mapping, approve it, and the vehicles reappear — the DLQ gets replayed. No redeploy."
**DO:** Trigger the drift: `python -m fs_simulator.publish --dry-run --force-drift` (or set `{"oem_d_drift": true}` in the control file at `./data/sim_control.json`, which `--control-file` reads). On the Platform screen, show the DLQ sample card, click **Preview** in the mapping editor, then approve the previewed version, then point at "Data quality per OEM" rising for OEM_D.
**CHECK:** DLQ sample shows OEM_D rows before approval; after approval, OEM_D's row count/quality figure updates without a page reload or restart.

### Beat 6 — Burst + broker kill, zero-loss reconciliation (2:52–3:28, 36s)
**GOAL:** Prove throughput, availability, and idempotency.
**ON SCREEN:** Command Center KPI strip (lag/throughput if shown) or terminal output from the chaos script; reconciliation report.
**SAY:** "Now a three-times traffic burst, and I'll kill a Kafka broker mid-stream. Consumer lag spikes, then drains as replicas take over — and the reconciliation report at the end shows zero events lost."
**DO:** Run `tests/chaos/kill-consumer.sh` (kills a consumer/broker per the chaos script) while a burst load is running via `tests/load/k6-api.js` (`k6 run tests/load/k6-api.js`). Show the terminal lag metric climbing then recovering, then open the reconciliation report output.
**CHECK:** Lag metric visibly spikes then returns to baseline; reconciliation report's lost-event count reads 0.
**NOTE:** `tests/chaos/kill-consumer.sh` kills a *consumer*, not a broker — the storyboard beat title says "kill a broker"; confirm which failure mode the script actually injects before claiming "broker kill" on camera, or rephrase SAY to "kill a consumer replica".

### Beat 7 — DuckDB over the lake, before/after optimization (3:28–3:58, 30s)
**GOAL:** Prove batch analytics at scale and SQL optimization work.
**ON SCREEN:** Terminal running the DuckDB query, then an EXPLAIN ANALYZE before/after pair.
**SAY:** "Our data lake holds hundreds of millions of telemetry rows in Parquet. This query scans them with DuckDB in a few seconds. And here's one of four query optimizations we made — index and materialized-view changes that cut this query from hundreds of milliseconds down to single digits."
**DO:** Run the lake query script against the seeded/full-profile lake (seed it beforehand with `python -m fs_simulator.seed --profile full` — this is the profile that builds the large lake, per `simulator/src/fs_simulator/seed.py`). Show its wall-clock output, then show one EXPLAIN ANALYZE pair side by side.
**CHECK:** Query completes and prints a row count and elapsed time on screen; EXPLAIN ANALYZE shows a lower cost/time after the optimization.
**NOTE:** Could not locate the specific "1B rows" DuckDB query script or the 4 EXPLAIN ANALYZE artifacts in this pass (spec.md §21 cites them, solution-document.md §9/§10 references `docs/evidence/` for load/chaos evidence but batch/DuckDB evidence location wasn't confirmed) — locate the actual script path (likely under `services/*/batch` or `ml/`) and the real row count/timing before finalizing SAY numbers; do not say "1 billion" unless the lake actually has that many rows.

### Beat 8 — Erasure and k-anonymous export (3:58–4:23, 25s)
**GOAL:** Prove privacy/compliance guarantees are real, not claimed.
**ON SCREEN:** Compliance (`web/src/pages/Compliance.tsx`) — "DSR request (erasure / access)" card, then "Privacy export (k-anonymity)" card.
**SAY:** "For a right-to-erasure request, we destroy the encryption key for that driver — replaying their old data now returns 'erased', not the original value. And exports for analytics are k-anonymous by construction."
**DO:** On Compliance, enter a driver ID in the DSR form, select the "erasure" kind button, submit. Then click **Load preview** on "Privacy export (k-anonymity)".
**CHECK:** DSR submission confirms; replaying/viewing that driver's prior record shows `[erased]`; the k-anonymity preview loads a non-empty, aggregated (not per-driver) table.

### Beat 9 — Audit chain + engineering rigour (4:23–4:40, 17s)
**GOAL:** Prove audit integrity and engineering evidence.
**ON SCREEN:** Compliance "Audit log" card with **Verify chain** button; then CI badge / coverage report / scan output.
**SAY:** "Every action here, human or agent, is in a hash-chained audit log — verified right now with one click. Our CI is green: four hundred plus tests, an eighty percent coverage gate, and clean security scans."
**DO:** Click **Verify chain** on the Compliance "Audit log" card. Switch to GitHub Actions / CI dashboard tab showing the green run and coverage %.
**CHECK:** "Verify chain" returns a success state (not a tamper warning); CI tab shows the latest run green with the coverage number visible.

### Beat 10 — Live URL + architecture, close (4:40–4:45, 5s + :15 buffer)
**GOAL:** Prove it's deployable, not just local.
**ON SCREEN:** Browser address bar showing the live cloud URL (Terraform VM, or the Cloudflare Tunnel fallback per `docs/demo-url-tunnel.md`), overlaid or followed by the architecture diagram.
**SAY:** "This is running live right now at this URL — same stack, deployable to any cloud with no code change. Thanks for watching."
**DO:** Switch to a tab already pointed at the live URL (pre-loaded before recording, do not navigate live) and hold for 3–4 seconds, then cut to the architecture diagram slide.
**CHECK:** The live URL tab is already loaded and responsive before this beat starts — never load it for the first time on camera.

---

## Time budget summary

| Beat | Title | Seconds | Cumulative |
|---|---|---|---|
| 1 | The problem in numbers | 18 | 0:18 |
| 2 | Command Center: live map + KPIs | 37 | 0:55 |
| 3 | Trigger a tow alert | 33 | 1:28 |
| 4 | Copilot: briefing, explain, approve, audit | 44 | 2:12 |
| 5 | Live OEM onboarding, zero downtime | 40 | 2:52 |
| 6 | Burst + broker kill, zero-loss reconciliation | 36 | 3:28 |
| 7 | DuckDB over the lake, before/after optimization | 30 | 3:58 |
| 8 | Erasure and k-anonymous export | 25 | 4:23 |
| 9 | Audit chain + engineering rigour | 17 | 4:40 |
| 10 | Live URL + architecture, close | 5 | 4:45 |
| — | Buffer | 15 | **5:00** |

## Flagged gaps (could not confirm against current code in this pass)

- **Beat 3 (tow trigger):** no dedicated CLI/API trigger for a tow-specific scenario was found in `simulator/src/fs_simulator/publish.py` — only generic drift/force-drift controls. Use a pre-seeded ground-truth incident and a known VIN instead of a live trigger, or confirm with whoever owns the simulator that such a trigger exists.
- **Beat 6 ("kill a broker"):** `tests/chaos/kill-consumer.sh` name suggests it kills a consumer, not a Kafka broker. Verify the actual failure mode before claiming "broker kill" on camera.
- **Beat 7 (1B-row DuckDB query + 4 EXPLAIN ANALYZE pairs):** script location and actual row count not confirmed in this pass. `simulator/src/fs_simulator/seed.py --profile full` is the seed profile most likely to build the large lake, but the exact row count and the EXPLAIN ANALYZE evidence files need to be located and their real numbers substituted before recording — do not say "1 billion" unless verified.
- **`docs/PROGRESS.md`** (last updated 2026-09-29) still lists milestones M3–M6 (API/auth/copilot real, feature freeze, evidence, demo video) as NOT STARTED/PARTIAL, while `docs/solution-document.md` §9–§13 describes a more complete state (403+ tests, STRIDE, crypto-shredding, k6/chaos scripts, live URL). These two documents disagree on current readiness — confirm actual current state before the recording session, since this script assumes the solution-document.md state is current.
