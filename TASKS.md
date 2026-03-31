# Task Board

> Managed by the Orchestrator. All agents read and write here.
> Status labels: [READY] · [BLOCKED: TASK-XX] · [IN PROGRESS] · [DONE] · [FAILED]

---

## In Progress

### TASK-03a · DB · [DONE]
**Goal:** Fix 3 database schema issues: (1) Add poll_timeout + partially_filled to order_events CHECK constraint, (2) Add missing columns to trade_decisions table so scanner writes succeed, (3) Add retry logic for PipelineTracer root row SSL failures.
**Acceptance:** All three constraints/schemas fixed. Scanner's trade_decision dict can be inserted without error.
**Output artifact:** SQL statements executed, column list confirmation.
**Shared file owner:** Supabase project vpollvsbtushbiapoflr
**Depends on:** TASK-01, TASK-02

### TASK-03b · BACKEND · [READY]
**Goal:** Add retry loop to PipelineTracer.__init__ for root pipeline_run creation (handles SSL timeouts). Fix scanner.py trade_decision dict to match the updated schema. Lint, syntax check, commit, push, deploy to ridley.
**Acceptance:** All files pass ruff + AST. Committed and pushed. Pulled on ridley via SSH.
**Output artifact:** Commit hash + ridley confirmation.
**Shared file owner:** scripts/tracer.py, scripts/scanner.py
**Depends on:** TASK-03a

---

## Completed

### TASK-01 · BACKEND · [DONE]
**Root cause:** poll_for_fill 60s timeout too short for Alpaca paper. Else branch didn't log anything.
**Fix:** Timeout bumped to 120s, else branch now logs poll_timeout event.

### TASK-02 · BACKEND · [DONE]
**Root cause:** SSL handshake timeout on root pipeline_run write. All subsequent FK-dependent writes failed silently. Morning scan ran fine but was invisible.
**Bonus:** Found trade_decisions schema mismatch and order_events CHECK blocking partially_filled + poll_timeout.
