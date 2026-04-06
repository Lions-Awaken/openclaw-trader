# Task Board

> Managed by the Orchestrator. All agents read and write here.
> Status labels: [READY] · [BLOCKED: TASK-XX] · [IN PROGRESS] · [DONE] · [FAILED]

---

## Audit Session: 2026-04-06 — Full Codebase Audit & Functionality Test

Context: CONGRESS_MIRROR profile went live today (first day, Sunday). yfinance + FRED data
sources wired into catalyst_ingest. Both scanner runs failed (NULL congress fields — fixed
in-session). This audit validates everything works. Monday 2026-04-07 runs are the live test;
go/no-go for Tuesday 2026-04-08 market open.

---

## Wave 1 — Critical Schema Fixes (before market open)

### TASK-A01 . DB-AGENT . [DONE]
**Goal:** Fix `inference_chains.stopping_reason` CHECK constraint — add `congress_signal_stale` value. The CONGRESS_MIRROR profile's `check_stopping_rule()` (inference_engine.py:810) can return `"congress_signal_stale"`, but the DB constraint only allows 8 values. Any trigger of this path will crash the chain finalization with a CHECK violation.
**Acceptance:** `SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'inference_chains'::regclass AND conname LIKE '%stopping%'` returns constraint including `congress_signal_stale`. Migration file written to `supabase/migrations/`.
**Output artifact:** Migration applied, constraint verified.
**Depends on:** nothing

### TASK-A02 . DB-AGENT . [READY]
**Goal:** Audit all CHECK constraints across all tables for code-vs-schema mismatches. The audit found `congress_signal_stale` missing — there may be others. Cross-reference every Python file that writes to Supabase against the CHECK constraint values. Specifically check: `cost_ledger.category` (does it cover all cost types?), `signal_evaluations.scan_type`, `signal_evaluations.decision`, `inference_chains.final_decision`.
**Acceptance:** Report listing every constraint checked, any mismatches found, and fixes applied. Zero constraint violations possible from current codebase.
**Output artifact:** Mismatch report in PROGRESS.md. Any new migration files if fixes needed.
**Depends on:** nothing

---

## Wave 2 — Code Quality Fixes (parallel, no dependencies)

### TASK-A03 . BACKEND-AGENT . [READY]
**Goal:** Fix bare `except` clauses in `common.py` (lines ~87-88, ~101-102 in `sb_get()` and `sb_rpc()`). These silently swallow all errors including network failures, auth errors, and schema mismatches. Add `except Exception as e:` with `print(f"[common] {function_name} error: {e}")` logging. Do NOT change the return behavior (still return `[]` or `None`).
**Acceptance:** `ruff check scripts/common.py` passes. No bare `except` clauses remain. Error messages are descriptive.
**Output artifact:** Updated common.py.
**Depends on:** nothing

### TASK-A04 . BACKEND-AGENT . [READY]
**Goal:** Add retry logic to `post_trade_analysis.py` `call_claude_postmortem()`. Currently a single Claude API failure means no post-trade analysis is generated. Match the retry pattern in `inference_engine.py` `call_claude()` — 2 attempts with ANTHROPIC_API_KEY, fallback to ANTHROPIC_API_KEY_2, log each attempt.
**Acceptance:** `call_claude_postmortem()` retries at least once on API failure before giving up. Ruff clean.
**Output artifact:** Updated post_trade_analysis.py.
**Depends on:** nothing

### TASK-A05 . BACKEND-AGENT . [READY]
**Goal:** Fix `scanner.py` `compute_signals()` returning `None` when fewer than 20 bars are available (line ~106). The caller iterates the result without null-checking. Add a guard: if `compute_signals()` returns `None` or empty, skip that ticker with a log message.
**Acceptance:** Scanner does not crash when a ticker has fewer than 20 bars. Ruff clean.
**Output artifact:** Updated scanner.py.
**Depends on:** nothing

---

## Wave 3 — Runtime Validation on Ridley (depends on Wave 1)

### TASK-A06 . BACKEND-AGENT . [BLOCKED: TASK-A01]
**Goal:** Dry-run `catalyst_ingest.py` on ridley to validate all 6 data sources work end-to-end. Run: `ssh ridley 'cd ~/openclaw-trader && source ~/.openclaw/workspace/.env && python3 scripts/catalyst_ingest.py'`. Verify: (1) yfinance source produces events, (2) FRED source produces events or gracefully skips (FRED data may not have changed today), (3) all 6 sources appear in Slack notification, (4) no errors in pipeline_runs.
**Acceptance:** Catalyst ingest completes successfully. `pipeline_runs` shows all steps as `success`. Slack notification includes yfinance and fred counts. At least 1 yfinance event inserted into `catalyst_events` with `source='yfinance'`.
**Output artifact:** Pipeline run ID and Slack notification screenshot/text in PROGRESS.md.
**Depends on:** TASK-A01 (schema must be fixed before any pipeline runs)

### TASK-A07 . BACKEND-AGENT . [BLOCKED: TASK-A01, TASK-A05]
**Goal:** Validate CONGRESS_MIRROR scanner flow end-to-end. This is a Sunday (market closed), so scanner will exit at `market_hours_check`. Verify by checking: (1) `strategy_profiles` has CONGRESS_MIRROR active, (2) `inference_chains` from today's failed runs have the right shape despite errors, (3) the NULL field fix (inference_engine.py:795) is deployed on ridley, (4) `congress_clusters` and `catalyst_events` with `source='quiverquant'` exist from today's catalyst ingest. Don't run the scanner — just verify data state.
**Acceptance:** Report confirming: active profile is CONGRESS_MIRROR, today's inference chains logged correctly (5 chains for PLTR/DELL/AVGO/NFLX/ARM), NULL fix is deployed, congress data pipeline is populated.
**Output artifact:** Data state report in PROGRESS.md.
**Depends on:** TASK-A01, TASK-A05

---

## Wave 4 — Security & Dashboard (parallel, no blockers)

### TASK-A08 . SECURITY-REVIEWER . [READY]
**Goal:** Full security scan of the openclaw-trader codebase. Check: (1) no secrets in tracked files (scan all .py, .html, .sh, .sql, .toml, .json), (2) no secrets in recent git commits (last 20 commits), (3) dependency CVE audit (`pip-audit` or manual check of pinned versions in dashboard/Dockerfile), (4) verify Supabase RLS policies are enforced (all tables have policies), (5) verify no direct SQL execution from user input paths, (6) check for command injection in any subprocess calls.
**Acceptance:** Security report with severity ratings. Zero CRITICAL or HIGH findings, or findings with documented mitigations.
**Output artifact:** Security audit report in PROGRESS.md.
**Depends on:** nothing

### TASK-A09 . FRONTEND-AGENT . [READY]
**Goal:** Verify dashboard deployment on Fly.io is current and functional. Check: (1) is the deployed version current with main? (`fly status -a openclaw-trader-dash`), (2) does the dashboard load? (hit /healthz), (3) do all 10 tabs render? (verify in browser via chrome tools), (4) does the Congress tab show data?, (5) does the Logging tab show domain cards with today's pipeline_runs data?, (6) are security headers present? (check response headers for X-Frame-Options, X-Content-Type-Options, CSP).
**Acceptance:** Dashboard is deployed, all tabs load, Congress + Logging tabs show real data, no JS console errors. Report any missing headers.
**Output artifact:** Dashboard health report in PROGRESS.md. Screenshots if browser tools available.
**Depends on:** nothing

---

## Wave 5 — Final Integration Verification (depends on all above)

### TASK-A10 . PICARD . [BLOCKED: TASK-A01 through TASK-A09]
**Goal:** Final integration review. Read all PROGRESS.md entries from this audit session. Verify: (1) all critical fixes are committed and deployed to ridley, (2) all schema changes are applied to live Supabase, (3) no failing pipeline_runs in last hour, (4) ridley crontab has correct schedule for Monday market open, (5) stash from ridley is applied or confirmed unnecessary. Monday's runs (2026-04-07) are the live validation — review those results and write a go/no-go assessment for Tuesday 2026-04-08 market open.
**Acceptance:** Written go/no-go assessment with specific items checked. Monday's pipeline_runs reviewed. All blockers resolved or documented.
**Output artifact:** Go/no-go assessment in PROGRESS.md.
**Depends on:** TASK-A01 through TASK-A09

---

## Stale Backlog (carried forward — not part of this audit)

### TASK-S47855759 · PICARD · [READY]
**Goal:** Rotate DASHBOARD_KEY on Fly.io.
**Depends on:** nothing

### TASK-S79014279 · PICARD · [READY]
**Goal:** Rotate DASHBOARD_KEY (duplicate of above).
**Depends on:** nothing

### TASK-S52706489 · PICARD · [READY]
**Goal:** Add daily P&L dashboard panel.
**Depends on:** nothing

---

## Completed (prior sessions)

### TASK-OC01 through TASK-OC04 · [DONE]
Logging & Observability Dashboard (2026-04-02)

### TASK-10 through TASK-18 · [DONE]
CONGRESS_MIRROR Profile Build (2026-04-01)

### TASK-01 through TASK-03b · [DONE]
Pipeline reliability fixes (2026-03-30)
