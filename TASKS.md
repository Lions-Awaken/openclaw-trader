# Task Board

> Managed by the Orchestrator. All agents read and write here.
> Status labels: [READY] . [BLOCKED: TASK-XX] . [IN PROGRESS] . [DONE] . [FAILED]

---

## Health Monitor + Signal Diversification Build

Source: Slack canvas F0ARMCN9KMF (thread ts 1775541385.146109)
Completion thread: 1775527228.672159
Supabase project: vpollvsbtushbiapoflr

Two parallel workstreams:
1. **System Health Monitor** — 34-check pre-market health script, system_health table, dashboard Health tab with indicator lights + Run Now, cron at 5AM PST weekdays
2. **Signal Diversification** — Options Flow + Form 4 Insider shadow profiles (5 total), ingest scripts, scanner enrichment, dashboard Signals tab, ingest crons

---

## Wave 1 — Schema (Data runs first)

### TASK-HM-01 . DB-AGENT . [DONE]
**Goal:** Create migration `supabase/migrations/20260407_system_health.sql` — add `system_health` table with columns: id (uuid PK), run_id (uuid NOT NULL), run_type (text CHECK scheduled/manual), check_group (text NOT NULL), check_name (text NOT NULL), check_order (integer NOT NULL), status (text CHECK pass/fail/warn/skip), value (text), expected (text), error_message (text), duration_ms (integer), created_at (timestamptz DEFAULT now()). Add 3 indexes: `idx_system_health_run_id(run_id, check_order)`, `idx_system_health_recent(created_at DESC)`, `idx_system_health_failures(status, created_at DESC) WHERE status IN ('fail','warn')`. Enable RLS with service-role-only policy. Apply to vpollvsbtushbiapoflr.
**Acceptance:** `SELECT COUNT(*) FROM system_health;` returns 0. Table has 12 columns. All 3 indexes exist. RLS enabled.
**Output artifact:** Migration file path + verification queries in PROGRESS.md.
**Depends on:** nothing

### TASK-SD-01 . DB-AGENT . [DONE]
**Goal:** Create migration `supabase/migrations/20260407_signal_diversification.sql` — (A) Create `options_flow_signals` table: id (uuid PK), ticker, signal_date, signal_type (CHECK unusual_call/unusual_put/sweep/block/darkpool), strike, expiry, premium, open_interest, volume, implied_volatility, sentiment (CHECK bullish/bearish/neutral), source (DEFAULT 'manual'), raw_data (jsonb), created_at. Indexes: `idx_options_flow_ticker(ticker, signal_date DESC)`, `idx_options_flow_recent(signal_date DESC, sentiment)`. RLS service-role-only. (B) Create `form4_signals` table: id (uuid PK), ticker, signal_date, filing_date, filer_name, filer_title, transaction_type (CHECK purchase/sale/gift/exercise), shares, price_per_share, total_value, shares_owned_after, ownership_pct_change, days_since_last_filing, cluster_count (DEFAULT 1), source (DEFAULT 'sec_edgar'), raw_data (jsonb), created_at. Indexes: `idx_form4_ticker(ticker, signal_date DESC)`, `idx_form4_purchases(transaction_type, signal_date DESC) WHERE transaction_type='purchase'`. RLS service-role-only. (C) Seed 2 new shadow profiles via INSERT ON CONFLICT DO NOTHING: OPTIONS_FLOW (shadow_type='SKEPTIC', min_signal_score=3, min_tumbler_depth=3, min_confidence=0.55, max_hold_days=5, is_shadow=true, active=false, dwm_weight=1.0, fitness_score=0.0) and FORM4_INSIDER (shadow_type='CONTRARIAN', min_signal_score=3, min_tumbler_depth=3, min_confidence=0.55, max_hold_days=15, is_shadow=true, active=false, dwm_weight=1.0, fitness_score=0.0). Apply to vpollvsbtushbiapoflr.
**Acceptance:** `SELECT profile_name, shadow_type, dwm_weight FROM strategy_profiles WHERE is_shadow = true ORDER BY profile_name;` returns 5 rows: CONTRARIAN, FORM4_INSIDER, OPTIONS_FLOW, REGIME_WATCHER, SKEPTIC. `SELECT COUNT(*) FROM options_flow_signals;` returns 0. `SELECT COUNT(*) FROM form4_signals;` returns 0. Both tables have correct indexes and RLS.
**Output artifact:** Migration file path + verification queries in PROGRESS.md.
**Depends on:** nothing

---

## Wave 2 — Core Scripts (Geordi, parallel — all touch different files)

### TASK-SD-04 . BACKEND-AGENT . [DONE]
**Goal:** Add OPTIONS_FLOW and FORM4_INSIDER entries to `scripts/shadow_profiles.py`. Add to SHADOW_SYSTEM_CONTEXTS dict (after REGIME_WATCHER, line ~37): OPTIONS_FLOW prompt emphasizing momentum, sweep/block/darkpool signals, 1-5 day alpha decay, IV rank, premium size, speed over depth. FORM4_INSIDER prompt emphasizing cluster buys, ownership pct change, CFO signal strength, 15-day hold, patience over speed. Add both to SHADOW_MAX_TUMBLER_DEPTH dict with value 5 (full depth). Full prompt text is in canvas F0ARMCN9KMF under TASK-SD-04.
**Acceptance:** `get_shadow_context('OPTIONS_FLOW')` returns non-empty string containing "options" and "sweep". `get_shadow_context('FORM4_INSIDER')` returns non-empty string containing "insider" and "cluster". `get_max_tumbler_depth('OPTIONS_FLOW') == 5`. `get_max_tumbler_depth('FORM4_INSIDER') == 5`. Ruff clean.
**Output artifact:** Updated shadow_profiles.py with 5 profile entries.
**Depends on:** TASK-SD-01

### TASK-HM-02 . BACKEND-AGENT . [DONE]
**Goal:** Create `scripts/health_check.py` — single self-contained script with 34 checks across 8 groups (Infrastructure 101-107, Database 201-207, Cron 301-304, Signals 401-405, Tumblers 501-506, Ensemble 601-606, Logging 701-705, Dashboard 801-805). Import from existing project scripts (common.py, inference_engine, scanner, shadow_profiles, tracer). Run modes: default (Slack on failure only), `--notify-always` (always post), `--group <name>` (single group), `--dry-run` (no DB write, no Slack). Write all results to `system_health` table grouped by `run_id`. Use `HEALTH_RUN_ID` env var if set (for dashboard trigger), else generate. Use colorama for colored output. Slack via `slack_notify()` from common.py. Full 34-check spec with exact check numbers, expected values, and scoring logic is in canvas F0ARMCN9KMF under TASK-HM-02.
**Acceptance:** `python scripts/health_check.py --dry-run` runs without error, prints colored results for all 34 checks. `python scripts/health_check.py --group infrastructure --dry-run` runs only group 1. Script imports cleanly from common.py (no new env vars, no inline httpx clients). Ruff clean.
**Output artifact:** New scripts/health_check.py. Sample --dry-run output in PROGRESS.md.
**Depends on:** TASK-HM-01

### TASK-SD-02 . BACKEND-AGENT . [DONE]
**Goal:** Create two new ingest scripts. (A) `scripts/ingest_options_flow.py` — Mode 1: reads CSV from `data/options_flow.csv`, writes to `options_flow_signals` table. Mode 2: stub `fetch_from_unusual_whales(api_key)` that prints warning if UNUSUAL_WHALES_API_KEY not set, returns empty list. Scoring function `score_options_signal(row) -> int` (1-10) based on premium size, signal type (sweep/block), IV rank, sentiment alignment. Uses `from common import sb_get, slack_notify` and `from tracer import traced`. Entry point: `if __name__ == "__main__"` with `@traced("ingest")`. (B) `scripts/ingest_form4.py` — Fetches SEC EDGAR Form 4 filings for target tickers (active watchlist + AI infrastructure: NVDA, AMD, AVGO, SMCI, MRVL, DELL, PLTR, ARM). Scoring function `score_form4_signal(row) -> int` (1-10) based on transaction type, total value, ownership pct change, cluster count, days since last filing, filer title. Uses common.py imports. Entry point with `@traced("ingest")`. Full scoring specs in canvas F0ARMCN9KMF under TASK-SD-02.
**Acceptance:** Both scripts importable without error. `score_options_signal()` returns int 1-10 for valid input. `score_form4_signal()` returns int 1-10 for valid input. Both use `from common import` pattern (no inline httpx/Supabase clients). Ruff clean.
**Output artifact:** New scripts/ingest_options_flow.py + scripts/ingest_form4.py.
**Depends on:** TASK-SD-01

### TASK-SD-03 . BACKEND-AGENT . [DONE]
**Goal:** Add two enrichment functions to `scripts/scanner.py`: `_enrich_with_options_flow(candidates)` — queries `options_flow_signals` for each candidate ticker (3-day lookback), adds `options_flow_bullish`, `options_flow_bearish`, `options_flow_net` to `cand["signals"]`. `_enrich_with_form4(candidates)` — queries `form4_signals` for each candidate ticker (14-day lookback, purchases only), adds `form4_insider_score`, `form4_purchase_count` to `cand["signals"]`. Insert both calls after the `signal_scan` tracer step (after line ~659), before the inference step (line ~661). Wrap in `with tracer.step("signal_enrichment")`. Full function signatures and query patterns in canvas F0ARMCN9KMF under TASK-SD-03.
**Acceptance:** Scanner runs without error when `options_flow_signals` and `form4_signals` tables are empty (enrichment adds 0 scores gracefully). Candidates dict has new signal keys after enrichment. No existing signal keys are modified. Ruff clean.
**Output artifact:** Updated scanner.py with enrichment functions and insertion point documented in PROGRESS.md.
**Depends on:** TASK-SD-01

---

## Wave 3 — Dashboard (Troi, sequential — both touch server.py + index.html)

### TASK-HM-03 . FRONTEND-AGENT . [DONE]
**Goal:** Add Health tab to dashboard. Server routes in `dashboard/server.py`: `GET /api/health/latest` (returns most recent run's results grouped by check_group, includes run_id, run_type, created_at, totals), `POST /api/health/run` (fires `health_check.py` as subprocess with HEALTH_RUN_ID env var, returns `{"status":"triggered","run_id":"<uuid>"}`). Frontend in `dashboard/index.html`: "Health" nav pill, auto-refresh every 30s. Layout: summary bar (pass/warn/fail counts + duration + last run time), "RUN NOW" button (cyan glow, POST to /api/health/run, poll /api/health/latest every 3s until new run_id), indicator light diagram (8 groups in pipeline order connected by arrows: INFRA -> DATABASE -> CRONS -> SIGNALS -> TUMBLERS -> ENSEMBLE -> LOGGING -> DASHBOARD, each check is a 40px circle with green pulse/red solid/yellow pulse/grey status, click to expand detail card), failures section (red-bordered cards with full error), 7-run history strip at bottom. Min 16px text, 20px values, 24px group headers. Orbitron font, dark bg, cyan/purple glow.
**Acceptance:** Health tab loads with data from system_health table. RUN NOW triggers health_check.py and polls for results. Indicator lights render in pipeline order. Clicking a light shows detail card. No JS errors. Font sizes >= 16px throughout.
**Output artifact:** Updated dashboard/server.py + dashboard/index.html.
**Depends on:** TASK-HM-02

### TASK-SD-05 . FRONTEND-AGENT . [DONE]
**Goal:** Add Signals tab to dashboard. Server routes in `dashboard/server.py`: `GET /api/signals/options_flow?days=7` (returns recent options_flow_signals rows), `GET /api/signals/form4?days=30` (returns recent form4_signals rows). Frontend in `dashboard/index.html`: "Signals" nav pill. Three sections: (1) Options Flow Feed — table with ticker, date, type, sentiment (color-coded), premium, IV, score. (2) Form 4 Insider Feed — table with ticker, filer, title, transaction type, total value, ownership change, cluster count. (3) Shadow Profile Comparison — 5-profile fitness chart (SKEPTIC, CONTRARIAN, REGIME_WATCHER, OPTIONS_FLOW, FORM4_INSIDER) showing fitness_score and dwm_weight as bars, colored by type. Min 16px text, Orbitron font, dark bg, cyan/purple glow.
**Acceptance:** Signals tab loads. Options flow table renders (may be empty). Form 4 table renders (may be empty). 5-profile fitness chart shows all 5 shadow profiles. No JS errors. Font sizes >= 16px throughout.
**Output artifact:** Updated dashboard/server.py + dashboard/index.html. Deployed to Fly.io.
**Depends on:** TASK-SD-01, TASK-HM-03

---

## Wave 4 — Cron + Deploy (Worf, after scripts exist)

### TASK-HM-04 . BACKEND-AGENT . [DONE]
**Goal:** Add health check crontab entry on ridley. Entry: `0 13 * * 1-5 cd /home/ridley/openclaw-trader && python scripts/health_check.py >> /tmp/openclaw_health.log 2>&1` (5AM PST = 13:00 UTC weekdays). Verify entry is correctly formatted for ridley's SHELL=/bin/bash crontab. Document the entry in PROGRESS.md.
**Acceptance:** `crontab -l` on ridley shows health_check.py entry at `0 13 * * 1-5`. Entry uses correct path `/home/ridley/openclaw-trader`.
**Output artifact:** Crontab entry documented in PROGRESS.md.
**Depends on:** TASK-HM-02

### TASK-SD-06 . BACKEND-AGENT . [DONE]
**Goal:** Add signal ingest crontab entries on ridley. Form 4: `0 14 * * 1-5 cd /home/ridley/openclaw-trader && python scripts/ingest_form4.py >> /tmp/openclaw_form4.log 2>&1` (6AM PST = 14:00 UTC weekdays). Options flow: `0 15 * * 1-5 cd /home/ridley/openclaw-trader && python scripts/ingest_options_flow.py >> /tmp/openclaw_options.log 2>&1` (7AM PST = 15:00 UTC weekdays). Document entries in PROGRESS.md.
**Acceptance:** `crontab -l` on ridley shows both ingest entries. Form 4 at `0 14 * * 1-5`, options flow at `0 15 * * 1-5`.
**Output artifact:** Crontab entries documented in PROGRESS.md.
**Depends on:** TASK-SD-02

---

## Wave 5 — Integration Verification

### TASK-INT-01 . PICARD . [DONE]
**Goal:** Final integration review. Run verification SQL: confirm 5 shadow profiles, 3 new tables (system_health, options_flow_signals, form4_signals), OPTIONS_FLOW and FORM4_INSIDER profiles present. Verify health_check.py --dry-run passes. Verify dashboard Health and Signals tabs load without JS errors. Verify Fly.io deployment is live. Post final summary to Slack thread 1775527228.672159.
**Acceptance:** All verification queries pass. Dashboard tabs functional. Fly.io deployment live. Slack summary posted.
**Output artifact:** Final summary in PROGRESS.md.
**Depends on:** TASK-HM-03, TASK-SD-05, TASK-HM-04, TASK-SD-06

---

## System Simulator + Enhanced Health Check Build

Source: In-session design (2026-04-07)
Goal: Full-system validation — every function tested end-to-end, every silent failure made loud

Two deliverables:
1. **System Simulator** — on-demand end-to-end test that exercises every pipeline with synthetic data, validates output, cleans up
2. **Enhanced Health Check** — 6 new check groups: Claude canary, budget pre-flight, crontab drift, output quality, data freshness, historical regression

---

## Wave 1 — Manifest Enhancement

### TASK-SIM-01 . BACKEND-AGENT . [DONE]
**Goal:** Enhance `scripts/manifest.py` with three new fields on ManifestEntry: (A) `output_validator: Callable[[dict], bool] | None` — a function that takes a pipeline_runs.output_snapshot dict and returns True if the output looks healthy. Examples: catalyst_ingest checks `total_inserted > 10`, scanner checks `candidates > 0`, meta_daily checks reflection content is not "Unable to assess". (B) `freshness_hours: int | None` — how many hours old the most recent pipeline_run for this entry can be before it's considered stale (e.g., catalyst_ingest=26 for "should have run in the last ~day", heartbeat=1). (C) `estimated_claude_cost: float` — expected Claude API cost per run in USD (e.g., scanner=0.03 for ~14 candidates x T4+T5, meta_daily=0.02, calibrator=0, health_check=0). Add validators for all 13 cron entries. Add a `validate_output(entry, snapshot) -> bool` helper and a `estimate_daily_claude_budget() -> float` helper that sums expected costs for a weekday. Ruff clean.
**Acceptance:** `estimate_daily_claude_budget()` returns a float > 0. Every cron entry has an `output_validator` that returns False for obviously bad output (empty, error strings). `validate_output(get_entry("catalyst_ingest"), {"total_inserted": 0})` returns False. `validate_output(get_entry("catalyst_ingest"), {"total_inserted": 50})` returns True. Ruff clean.
**Output artifact:** Updated scripts/manifest.py.
**Depends on:** nothing

---

## Wave 2 — Simulator + Health Check Enhancements (parallel — different files)

### TASK-SIM-02 . BACKEND-AGENT . [DONE]
**Goal:** Create `scripts/test_system.py` — NASA-style go/no-go preflight system simulator. Supersedes TASK-S95373659.

**Dual-mode operation:**
- CLI: `python scripts/test_system.py` — colorama terminal output, live status updates
- Dashboard-triggered: `SIMULATOR_RUN_ID=<uuid>` env var — writes each test result to `system_health` table (run_type='simulator') IMMEDIATELY on completion, enabling live dashboard polling

**Live-write contract:** Each test writes to `system_health` the MOMENT it finishes (not batched). Fields: run_id, run_type='simulator', check_group, check_name, check_order, status, value, expected, error_message, duration_ms. The dashboard polls every 2s and renders each test transitioning from PENDING to GO/NO-GO in real time.

**Visual design — NASA go/no-go format:**

CLI output:
```
=====================================
  OPENCLAW PREFLIGHT — GO/NO-GO
  2026-04-07 20:15:03 PDT
=====================================

  FLIGHT DIRECTOR .......... STANDBY
  ─────────────────────────────────

  A · MODULE INTEGRITY
  [A1] manifest imports ......... GO   16/16 modules    (120ms)
  [A2] function signatures ...... GO   42 callable      (45ms)

  B · GROUND SYSTEMS (SCHEMA)
  [B1] table inventory .......... GO   30/30 tables     (89ms)
  [B2] shadow_divergences ....... GO   22 columns       (34ms)
  [B3] shadow profiles .......... GO   5 seeded         (41ms)
  [B4] profile_name backfill .... GO   158 chains       (56ms)
  [B5] signal tables ............ GO   2 tables ready   (28ms)
  [B6] system_health table ...... GO   12 columns       (23ms)

  C · ADVERSARIAL ARRAY
  [C1] shadow contexts .......... GO   5/5 non-empty    (12ms)
  [C2] tumbler depth caps ....... GO   RW=3 others=5    (8ms)

  D · SIGNAL ACQUISITION
  [D1] active profile ........... GO   CONGRESS_MIRROR  (67ms)
  [D2] compute_signals .......... GO   score=4          (156ms)
  [D3] options flow enrich ...... GO   +3 signal keys   (23ms)
  [D4] form4 enrich ............. GO   +2 signal keys   (19ms)

  E · TUMBLER CHAIN
  [E1] inference (SKEPTIC) ...... GO   decision=watch   (2.3s)
  [E2] depth cap (RW) ........... GO   stopped at T3    (1.8s)
  [E3] stopping rule null ....... GO   no TypeError     (5ms)
  [E4] shadow context inject .... GO   prompt verified  (12ms)

  F · ENSEMBLE SYSTEMS
  [F1] load shadow profiles ..... GO   5 loaded         (45ms)
  [F2] record divergence ........ GO   write+verify+del (89ms)
  [F3] grade profiles ........... GO   {graded:0}       (34ms)
  [F4] divergence summary ....... GO   4 keys present   (23ms)

  G · ECONOMICS
  [G1] claude spend today ....... GO   $0.04            (56ms)
  [G2] claude budget ............ GO   $0.50 (92% left) (34ms)
  [G3] cost attribution ......... GO   profile in subcat (8ms)
  [G4] daily budget estimate .... GO   $0.12 needed     (5ms)

  H · END-TO-END FLOW
  [H1] inject test catalyst ..... GO   SIM_TEST created (34ms)
  [H2] signal scan .............. GO   signals computed (156ms)
  [H3] enrichment pipeline ...... GO   5 new keys       (42ms)
  [H4] inference chain .......... GO   chain stored     (2.1s)
  [H5] divergence record ........ GO   row verified     (67ms)
  [H6] cleanup .................. GO   5 rows deleted   (89ms)

  I · DASHBOARD COMMS
  [I1] /api/shadow/profiles ..... GO   5 profiles       (123ms)
  [I2] /api/shadow/divergences .. GO   valid list       (89ms)
  [I3] /api/health/latest ....... GO   200 OK           (67ms)
  [I4] /api/signals/options-flow  GO   200 OK           (78ms)
  [I5] /api/signals/form4 ....... GO   200 OK           (45ms)

  ─────────────────────────────────
  FLIGHT DIRECTOR .......... ALL GO

=====================================
  35/35 GO  |  0 NO-GO  |  0 SCRUB
  T+ 14.2s
=====================================
```

On failure, a NO-GO test shows inline error:
```
  [E1] inference (SKEPTIC) ...... NO-GO
        Error: AttributeError — module has no attribute '_active_profile'
        Expected: chain with profile_name='SKEPTIC'
```

**Test groups in pipeline execution order:**

GROUP A — MODULE INTEGRITY (check_order 100-199): Import all manifest scripts (A1), verify key functions callable (A2)
GROUP B — GROUND SYSTEMS (check_order 200-299): 30 tables exist (B1), shadow_divergences 22 cols (B2), 5 shadow profiles (B3), profile_name backfilled (B4), signal tables (B5), system_health table (B6)
GROUP C — ADVERSARIAL ARRAY (check_order 300-399): shadow contexts valid for 5 types (C1), tumbler depth caps correct (C2)
GROUP D — SIGNAL ACQUISITION (check_order 400-499): load_strategy_profile (D1), compute_signals with synthetic bars (D2), options flow enrichment (D3), form4 enrichment (D4)
GROUP E — TUMBLER CHAIN (check_order 500-599): run_inference SKEPTIC override (E1), REGIME_WATCHER depth cap (E2), stopping rule null guard (E3), shadow context injection (E4)
GROUP F — ENSEMBLE SYSTEMS (check_order 600-699): load shadow profiles (F1), record+verify+delete divergence (F2), grade with 0 data (F3), divergence summary structure (F4)
GROUP G — ECONOMICS (check_order 700-799): claude spend (G1), claude budget (G2), cost attribution format (G3), daily budget estimate (G4)
GROUP H — END-TO-END FLOW (check_order 800-899): inject catalyst (H1), signal scan (H2), enrichment (H3), inference chain (H4), divergence record (H5), cleanup all SIM_TEST rows (H6)
GROUP I — DASHBOARD COMMS (check_order 900-999): 5 API endpoint checks (I1-I5)

Clean up ALL synthetic data (ticker='SIM_TEST') in H6. `--dry-run` skips DB writes, external API calls, and dashboard checks.

**Acceptance:** `python scripts/test_system.py` runs ~35 tests in go/no-go format, writes each result to system_health as it completes (when SIMULATOR_RUN_ID set), cleans up synthetic data, exits 0 when all GO. Ruff clean.
**Output artifact:** New scripts/test_system.py. Sample output in PROGRESS.md.
**Depends on:** TASK-SIM-01

### TASK-SIM-03 . BACKEND-AGENT . [DONE]
**Goal:** Add 6 new check groups to `scripts/health_check.py`, leveraging the enhanced manifest:

**GROUP 9 — Claude API (order 900-999)** — 3 checks
901: Claude API canary — make one cheap Claude haiku call ("Reply with the word HEALTHY"), assert response contains "HEALTHY". Print latency. If fails, entire day's Claude-dependent pipelines will fail.
902: Budget pre-flight — call get_claude_budget() and get_todays_claude_spend(), compute remaining. Call estimate_daily_claude_budget() from manifest. Assert remaining >= estimated daily need. Print: budget=$X, spent=$Y, remaining=$Z, needed=$W.
903: Claude API key valid — assert CLAUDE_API_KEY env var is set and length > 20 (don't print value).

**GROUP 10 — Crontab Drift (order 1000-1099)** — 2 checks
1001: Crontab vs manifest — run `crontab -l`, parse entries, compare against manifest schedules. For each manifest entry with a cron schedule, assert a matching crontab line exists. Report any manifest entries missing from crontab and any crontab entries not in manifest.
1002: Script files on disk — for each manifest entry, assert the script file exists at the path specified. This catches the "code not pulled to ridley" bug.

**GROUP 11 — Output Quality (order 1100-1199)** — 3 checks
1101: Yesterday's pipeline output validation — for each manifest entry that should have run yesterday (based on schedule + day of week), query the most recent pipeline_runs.output_snapshot and run the entry's output_validator. Flag any that return False.
1102: Meta reflection quality — query today's (or most recent) meta_reflections. Assert signal_assessment is not "Unable to assess" and length > 50.
1103: Catalyst source diversity — query most recent catalyst_ingest output_snapshot. Assert at least 3 sources produced > 0 events.

**GROUP 12 — Data Freshness (order 1200-1299)** — 4 checks
1201: Catalyst events fresh — assert catalyst_events has rows from < freshness_hours (from manifest entry).
1202: Inference chains fresh — assert inference_chains has rows from < 26 hours on weekdays.
1203: Pipeline runs fresh — for each high-criticality manifest entry, assert pipeline_runs has a row within freshness_hours.
1204: Shadow divergences flowing — assert shadow_divergences has rows from < 26 hours on weekdays (confirms ensemble is producing data).

**GROUP 13 — Historical Regression (order 1300-1399)** — 3 checks
1301: Catalyst volume regression — query last 20 catalyst_ingest root output_snapshots, compute average total_inserted. Assert today's (or most recent) is within 50% of average. Print: avg=X, today=Y.
1302: Scanner candidate regression — same pattern for scanner candidates count.
1303: Shadow divergence rate — compute divergence rate (divergences / total shadow inferences) over last 7 days. Assert rate is between 5% and 80% (too low = shadows always agree = not adversarial enough, too high = shadows always disagree = not calibrated).

**Acceptance:** `python scripts/health_check.py --dry-run` runs all original + new groups without error. `--group claude_api --dry-run` runs only group 9. New checks individually wrapped in try/except (one failure doesn't crash others). All checks import from manifest.py for validators/freshness/budget. Ruff clean.
**Output artifact:** Updated scripts/health_check.py with groups 9-13.
**Depends on:** TASK-SIM-01

---

## Wave 3 — Dashboard Flight Status

### TASK-SIM-04 . FRONTEND-AGENT . [DONE]
**Goal:** Add NASA-style Go/No-Go Preflight panel to the dashboard with a manual "RUN PREFLIGHT" trigger button and live visual status updates.

**New API routes in `dashboard/server.py`:**

`GET /api/health/flight-status` — queries pipeline_runs for each manifest entry (last 24h or freshness_hours), returns per-entry status: {name, script, schedule_desc, pipeline_name, criticality, last_run_at, last_status, output_valid, freshness_ok}.

`POST /api/simulator/run` — fires `scripts/test_system.py` as a subprocess with `SIMULATOR_RUN_ID` env var set. Returns `{"status": "triggered", "run_id": "<uuid>"}`.

`GET /api/simulator/status?run_id=<uuid>` — queries `system_health WHERE run_id=<uuid> AND run_type='simulator'` ordered by check_order. Returns all test results written so far (the simulator writes each result as it completes, so this endpoint shows live progress).

**Dashboard UI — new "Preflight" tab (or section within Health tab):**

Design: NASA Mission Control go/no-go aesthetic. Dark background, monospace-style readout, green/red/amber status indicators.

**Layout — top section:**
```
================================================
  OPENCLAW PREFLIGHT — GO / NO-GO
  Last run: 2026-04-07 20:15:03 PDT
  Status: ALL STATIONS GO
================================================

              [ INITIATE PREFLIGHT ]
```

Big "INITIATE PREFLIGHT" button (green glow, large padding). On click: POST /api/simulator/run, show "PREFLIGHT SEQUENCE INITIATED", begin polling /api/simulator/status?run_id=X every 2 seconds.

**Layout — main panel (the go/no-go board):**

Each test group is a "station." Each test within a group is a "subsystem." Displayed as a vertical list in pipeline execution order. Each row shows:

```
STATION          SUBSYSTEM                    STATUS     VALUE          TIME
─────────────────────────────────────────────────────────────────────────
MODULE INTEGRITY
                 manifest imports              GO        16/16 modules   120ms
                 function signatures            GO        42 callable      45ms
GROUND SYSTEMS
                 table inventory                GO        30/30 tables     89ms
                 shadow_divergences             GO        22 columns       34ms
                 shadow profiles                GO        5 seeded         41ms
                 ...
TUMBLER CHAIN
                 inference (SKEPTIC)           POLLING    ...
                 depth cap (RW)               STANDBY
                 ...
```

**Status indicators per test (large, clear):**
- `STANDBY` — grey dot, test hasn't started yet (queued)
- `POLLING` — blue spinning indicator, test is currently running
- `GO` — bright green dot + "GO" text, test passed
- `NO-GO` — bright red dot + "NO-GO" text, test failed. Clicking expands inline to show full error message, expected value, and actual value
- `SCRUB` — amber dot, test was skipped

**Polling behavior:**
- After clicking INITIATE PREFLIGHT, poll /api/simulator/status every 2 seconds
- Render ALL ~35 tests as STANDBY initially
- As results come in from the poll, update each test row from STANDBY to GO/NO-GO
- Tests are ordered by check_order so they light up in sequence (imports first, then schema, then signal chain, etc.)
- When all tests have a result (or 120s timeout), show final summary: "ALL STATIONS GO" or "NO-GO — X failures"
- Stop polling

**Flight Status section (below the go/no-go board):**
Shows the manifest-vs-reality diff for today's scheduled runs. One row per manifest entry:
- Name, schedule, last fired (relative time ago)
- Status dot: green = ran + output valid, yellow = ran + output failed validation, red = didn't run within freshness window, grey = not scheduled today
- Click to expand: output_snapshot details, validator result

**Design requirements (CRITICAL):**
- Min 16px for all text, 20px for status values, 24px for station headers
- Orbitron font for headers, monospace for the data grid
- Dark background, green/red glow effects matching sci-fi dashboard aesthetic
- Auto-refresh Flight Status every 30 seconds (independent of simulator polling)

**Acceptance:** INITIATE PREFLIGHT button triggers simulator and shows live go/no-go updates as each test completes. Tests transition from STANDBY to POLLING to GO/NO-GO in pipeline order. NO-GO tests show error details on click. Flight Status shows today's manifest vs actual. No JS errors. Font sizes >= 16px.
**Output artifact:** Updated dashboard/server.py + dashboard/index.html.
**Depends on:** TASK-SIM-02, TASK-SIM-03

---

## Wave 4 — Integration + Deploy

### TASK-SIM-05 . PICARD . [BLOCKED: TASK-SIM-02, TASK-SIM-04]
**Goal:** Final integration. (1) Commit all changes and push to git. (2) SSH to ridley, git pull, verify all new scripts exist. (3) Run `python scripts/test_system.py` on ridley — all tests should pass. (4) Run `python scripts/health_check.py --dry-run` on ridley — all groups including new ones should pass. (5) Verify dashboard Flight Status tab loads with correct data. (6) Deploy dashboard to Fly.io. (7) Post summary to Slack thread 1775527228.672159.
**Acceptance:** test_system.py passes all tests on ridley. health_check.py --dry-run passes on ridley. Dashboard deployed. Slack summary posted. All scripts exist on ridley.
**Output artifact:** Final summary in PROGRESS.md with test output.
**Depends on:** TASK-SIM-02, TASK-SIM-04

---

## Completed — Prior Sessions

### Health Monitor + Signal Diversification (2026-04-07): TASK-HM-01 through TASK-SD-06 + TASK-INT-01 . [DONE]
### Adversarial Ensemble Architecture (2026-04-06): TASK-AE-01 through TASK-AE-07 . [DONE]
### Dashboard Fix Session (2026-04-06): TASK-D01 through TASK-D06 . [DONE]
### Audit Session (2026-04-06): TASK-A01 through TASK-A10 . [DONE]
### Logging & Observability (2026-04-02): TASK-OC01 through TASK-OC04 . [DONE]
### CONGRESS_MIRROR Build (2026-04-01): TASK-10 through TASK-18 . [DONE]
### Pipeline Reliability (2026-03-30): TASK-01 through TASK-03b . [DONE]

## Backlog

### TASK-S67695479 · PICARD · [READY]
**Goal:** Two remaining tasks from earlier audit run. A03/A04/A05 already done in repo. Just need these two. Post completion back to thread ts 1775575782.866409.
TASK-A02 · DB-AGENT · CHECK constraint audit
Query pg_constraint on Supabase project vpollvsbtushbiapoflr for all CHECK constraints across all tables. Cross-reference against every string value written to constrained columns in the Python codebase — specifically cost_ledger.category, signal_evaluations.scan_type, signal_evaluations.decision, inference_chains.final_decision. Report every mismatch. Write migration files to supabase/migrations/ for any fixes needed. Report findings in PROGRESS.md.
Acceptance: zero CHECK violations possible from current codebase.
TASK-QQ · BACKEND-AGENT · QuiverQuant 0-event root cause
Today's catalyst ingest logged quiverquant: 0. Find the QQ fetch function in scripts/catalyst_ingest.py. Check what endpoint and auth it uses and how errors are handled. Test the fetch in isolation to determine if it is a rate limit, auth failure, or legitimately no new congressional trades today. Check pipeline_runs for today's catalyst_ingest quiverquant step for any error messages. If errors are being silently swallowed, fix the error handling. Document root cause clearly in PROGRESS.md.
Acceptance: root cause identified and documented. Error handling improved if applicable. *Sent using* Claude
**Acceptance:** Task completed as described. Results posted to #all-lions-awaken thread.
**Source:** [Slack dispatch — 2026-04-07 15:48 UTC](https://lions-awaken.slack.com/archives/C0ANK2A0M7G/p1775576867695479)
**Depends on:** nothing

### TASK-S82866409 · PICARD · [READY]
**Goal:** Five-task audit cleanup + QuiverQuant investigation. Brian is remote — dispatching via <http://Claude.ai|Claude.ai>. Run /layinacourse first. Post each completion back to this thread.
TASK-A02 · DB-AGENT · CHECK constraint audit
Cross-reference all Python files writing to Supabase against all CHECK constraint values in project vpollvsbtushbiapoflr. Query pg_constraint for all check constraints, then grep codebase for every string written to cost_ledger.category, signal_evaluations.scan_type, signal_evaluations.decision, inference_chains.final_decision. Report every mismatch. Write migration files to supabase/migrations/ for any fixes. Report in PROGRESS.md.
TASK-A03 · BACKEND-AGENT · Bare except in common.py
Find and fix all bare except: clauses in scripts/common.py. Replace with except Exception as e: and add print(f"[common] {fn_name} error: {e}"). Do NOT change return behavior (still return [] or None). Acceptance: ruff check scripts/common.py passes, zero bare excepts remain.
TASK-A04 · BACKEND-AGENT · Retry logic in post_trade_analysis.py
Add retry logic to call_claude_postmortem(). Match the pattern in inference_engine.py call_claude() — 2 attempts with ANTHROPIC_API_KEY, fallback to ANTHROPIC_API_KEY_2, log each attempt. Acceptance: retries at least once before giving up. Ruff clean.
TASK-A05 · BACKEND-AGENT · compute_signals() null guard in scanner.py
compute_signals() can return None when fewer than 20 bars are available. The caller iterates the result without null-checking — crash path. Add guard: if result is None or empty, skip ticker with descriptive log. Acceptance: no crash on thin-data tickers. Ruff clean.
TASK-QQ · BACKEND-AGENT · QuiverQuant 0-event investigation
Today's catalyst ingest logged quiverquant: 0. CONGRESS_MIRROR depends on this source. Investigate: (1) read scripts/catalyst_ingest.py — find the QQ fetch function, what endpoint, what auth, how errors are handled. (2) Run the fetch in isolation or curl the endpoint to test if it's a rate limit, auth failure, or legitimately empty. (3) Check pipeline_runs for today's catalyst_ingest quiverquant step — any error messages? (4) If silent error being swallowed: fix error handling so failures are visible. If rate limit or API key issue: document it clearly. Root cause report in PROGRESS.md.
Ruff-check all modified Python files before marking any task done. *Sent using* Claude
**Acceptance:** Task completed as described. Results posted to #all-lions-awaken thread.
**Source:** [Slack dispatch — 2026-04-07 15:30 UTC](https://lions-awaken.slack.com/archives/C0ANK2A0M7G/p1775575782866409)
**Depends on:** nothing

### TASK-S82507299 . PICARD . [READY]
**Goal:** Research only — no file changes. Answer all questions and post back here.
_Q1 — crontab_ Run `crontab -l` and paste the full output.
_Q2 — scripts inventory_ Run `ls scripts/*.py` and paste the full list.
_Q3 — script entry points_ For each of these scripts, show the first 5 lines of the `if __name__ == "__main__":` block so I know how each is invoked and what args it accepts: `scanner.py`, `pre_market.py`, `meta_daily.py`, `calibrator.py`. If any of those files don't exist, say so explicitly.
_Q4 — pipeline_runs step names_ Run this SQL against Supabase `vpollvsbtushbiapoflr`: `SELECT DISTINCT pipeline_name, step_name FROM pipeline_runs ORDER BY pipeline_name, step_name;` — paste the full result.
_Q5 — system_stats table_ Run: `SELECT DISTINCT metric_name FROM system_stats LIMIT 50;` — paste the result so I know what metrics are already being tracked.
_Q6 — stack_heartbeats table_ Run: `SELECT * FROM stack_heartbeats ORDER BY created_at DESC LIMIT 5;` — paste the result.
_Q7 — dashboard server routes_ In `dashboard/server.py`, list every `@app.get` and `@app.post` route (just the path strings, no need for full function bodies).
_Q8 — dashboard port_ What port does the dashboard server run on? Is it behind a reverse proxy or direct?
Post answers as structured replies. No code changes.
**Acceptance:** Task completed as described. Results posted to #all-lions-awaken thread.
**Source:** [Slack dispatch — 2026-04-07 04:39 UTC](https://lions-awaken.slack.com/archives/C0ANK2A0M7G/p1775536682507299)
**Depends on:** nothing

### TASK-S95373659 . PICARD . [READY]
**Goal:** Build `scripts/test_adversarial_ensemble.py` — a standalone end-to-end simulator for the adversarial ensemble system.
**Acceptance:** Task completed as described. Results posted to #all-lions-awaken thread.
**Source:** [Slack dispatch — 2026-04-07 04:07 UTC](https://lions-awaken.slack.com/archives/C0ANK2A0M7G/p1775534795373659)
**Depends on:** nothing
