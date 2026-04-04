# Task Board

> Managed by the Orchestrator. All agents read and write here.
> Status labels: [READY] · [BLOCKED: TASK-XX] · [IN PROGRESS] · [DONE] · [FAILED]

---

## In Progress

_(none)_

---

## Logging & Observability Dashboard

### TASK-OC01 · DATA · [DONE]
**Goal:** Add `@traced("domain")` decorator and thread-local active tracer management to `scripts/tracer.py`. Hook into PipelineTracer lifecycle (set on init, clear on complete/fail).
**Acceptance:** Self-test demonstrates decorator works within pipeline context and is a no-op without one. No new DB tables needed.
**Output artifact:** Updated tracer.py with `traced()`, `set_active_tracer()`, `get_active_tracer()`, `clear_active_tracer()`.
**Depends on:** nothing

### TASK-OC02 · GEORDI · [DONE]
**Goal:** Instrument ~45 functions across 11 scripts with `@traced("domain")` decorators. Add `set_active_tracer(tracer)` to all `run()` functions. Add PipelineTracer to heartbeat.py.
**Acceptance:** All scripts run without error. No duplicate tracing. `pipeline_runs` table gets domain-prefixed step entries on next cron cycle.
**Depends on:** TASK-OC01

### TASK-OC03 · GEORDI · [DONE]
**Goal:** Add 3 API endpoints to `dashboard/server.py`: GET /api/logs/domains (24h summary), GET /api/logs/domain/{name} (per-function history), POST /api/trades/{id}/reasoning (AI analysis with caching).
**Acceptance:** All endpoints return correct data. Reasoning endpoint caches in trade_decisions.metadata and rate-limits to 10/hour.
**Depends on:** TASK-OC01

### TASK-OC04 · GEORDI · [DONE]
**Goal:** Add Logging dashboard page to `index.html` + `theme.css`. 8 domain cards with notification badges, click-to-expand modal with per-function run history, trade reasoning AI section.
**Acceptance:** Cards render with real badge counts. Modal shows function-level data. Trade reasoning works end-to-end. Matches sci-fi theme. All text XSS-escaped.
**Depends on:** TASK-OC03

---

## Backlog

### TASK-S47855759 · PICARD · [READY]
**Goal:** Urgent — rotate the DASHBOARD_KEY on <http://Fly.io|Fly.io> right now. Single task, nothing else. Run: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` to generate the key, then `fly secrets set DASHBOARD_KEY=&lt;key&gt; --app openclaw-trader-dash`. Post the new key in this thread immediately when done. Brian is locked out of the dashboard. *Sent using* Claude
**Acceptance:** Task completed as described. Results posted to #all-lions-awaken thread.
**Source:** [Slack dispatch — 2026-04-03 15:08 UTC](https://lions-awaken.slack.com/archives/C0ANK2A0M7G/p1775228847855759)
**Depends on:** nothing

### TASK-S79014279 · PICARD · [READY]
**Goal:** Rotate the DASHBOARD_KEY secret on <http://Fly.io|Fly.io> for the openclaw-trader-dash app. Generate a new secure random key with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`, set it with `fly secrets set DASHBOARD_KEY=&lt;new-key&gt; --app openclaw-trader-dash`, verify the redeploy completes cleanly, and post the new key back to this thread so Brian can log in. Thread only — do NOT post as a top-level message. *Sent using* Claude
**Acceptance:** Task completed as described. Results posted to #all-lions-awaken thread.
**Source:** [Slack dispatch — 2026-04-03 14:40 UTC](https://lions-awaken.slack.com/archives/C0ANK2A0M7G/p1775227179014279)
**Depends on:** nothing

### TASK-S52706489 · PICARD · [READY]
**Goal:** Add a new dashboard panel for daily P&amp;L *Sent using* Claude
**Acceptance:** Task completed as described. Results posted to #all-lions-awaken thread.
**Source:** [Slack dispatch — 2026-04-03 05:51 UTC](https://lions-awaken.slack.com/archives/C0ANK2A0M7G/p1775195452706489)
**Depends on:** nothing

_(none)_

---

## Completed

### TASK-10 · DB-AGENT · [DONE]
**Goal:** Create migration for CONGRESS_MIRROR profile: 3 new tables, catalyst_events extension, strategy profile seed.
**Output:** `/home/mother_brain/projects/openclaw-trader/supabase/migrations/20260401_congress_profile.sql`
**Note:** Migration file written. Needs to be applied to live Supabase project vpollvsbtushbiapoflr.

### TASK-11 · BACKEND-AGENT · [DONE]
**Goal:** Create seed script for 10 high-signal congress members.
**Output:** `/home/mother_brain/projects/openclaw-trader/scripts/seed_politician_intel.py`
**Note:** 10 politicians seeded. Pelosi at 0.85. All scores >= 0.45.

### TASK-12 · BACKEND-AGENT · [DONE]
**Goal:** Enhance catalyst_ingest.py with politician scoring, freshness scoring, cluster detection.
**Output:** `/home/mother_brain/projects/openclaw-trader/scripts/catalyst_ingest.py`
**Functions added:** `load_politician_scores()`, `score_disclosure_freshness()`, `detect_congress_clusters()`, `classify_ticker_sector()`, `TICKER_SECTOR_MAP`. Modified: `fetch_quiverquant_trades()` (enrichment), `run()` (cluster step).

### TASK-13 · BACKEND-AGENT · [DONE]
**Goal:** Create legislative calendar fetcher script.
**Output:** `/home/mother_brain/projects/openclaw-trader/scripts/legislative_calendar.py`
**Note:** Fetches from Congress.gov API + Perplexity. Handles missing API keys gracefully.

### TASK-14 · BACKEND-AGENT · [DONE]
**Goal:** Add congress-mode behavior to inference engine Tumbler 2.
**Output:** `/home/mother_brain/projects/openclaw-trader/scripts/inference_engine.py`
**Functions added:** `get_legislative_context()`, `get_congress_cluster_context()`. Modified: `tumbler_2_fundamental()` (congress boost), `check_stopping_rule()` (congress_signal_stale + ticker param).

### TASK-15 · BACKEND-AGENT · [DONE]
**Goal:** Add congress watchlist branch to scanner.
**Output:** `/home/mother_brain/projects/openclaw-trader/scripts/scanner.py`
**Functions added:** `build_congress_watchlist()`. Modified: `run()` step 4 build_watchlist (congress branch with fallback).

### TASK-16 · BACKEND-AGENT · [DONE]
**Goal:** Add 4 congress API endpoints to dashboard server.
**Output:** `/home/mother_brain/projects/openclaw-trader/dashboard/server.py`
**Endpoints:** GET /api/congress/politicians, /api/congress/signals, /api/congress/clusters, /api/congress/calendar.

### TASK-17 · FRONTEND-AGENT · [DONE]
**Goal:** Add Congress tab to dashboard UI.
**Output:** `/home/mother_brain/projects/openclaw-trader/dashboard/index.html`
**Added:** Nav pill, section-congress div (4 cards), 5 JS functions (loadCongressTab, loadCongressLeaderboard, loadCongressSignals, loadCongressClusters, loadCongressCalendar).

### TASK-18 · BACKEND-AGENT · [DONE]
**Goal:** Document crontab additions.
**Output:** `/home/mother_brain/projects/openclaw-trader/docs/congress-crontab-additions.md`

### TASK-01 · BACKEND · [DONE]
**Root cause:** poll_for_fill 60s timeout too short for Alpaca paper. Else branch didn't log anything.
**Fix:** Timeout bumped to 120s, else branch logs poll_timeout event.

### TASK-02 · BACKEND · [DONE]
**Root cause:** SSL handshake timeout killed root pipeline_run row. All FK-dependent writes cascaded to failure. Scanner ran but was invisible in DB.
**Bonus:** Found trade_decisions schema mismatch (12 missing columns) and order_events CHECK blocking poll_timeout/partially_filled.

### TASK-03a · DB · [DONE]
**Fix:** order_events CHECK expanded (3 new event types). trade_decisions got 12 missing columns + legacy NOT NULL defaults. Migration file written.

### TASK-03b · BACKEND · [DONE]
**Fix:** Tracer retry loop (3 attempts, 2s delay) for root pipeline_run creation. All code verified, committed (966aff1), pushed, deployed to ridley.
