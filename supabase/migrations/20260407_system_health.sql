-- =============================================================================
-- System Health Monitor — Schema Foundation
--
-- Creates the system_health table to store results from health_check.py,
-- a 34-check pre-market diagnostic script. Each script run groups its
-- checks under a shared run_id so the dashboard can display pass/fail
-- status per run and per check group.
--
-- Applied: vpollvsbtushbiapoflr — 2026-04-07
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.system_health (
  id            uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
  run_id        uuid        NOT NULL,
  run_type      text        NOT NULL CHECK (run_type IN ('scheduled', 'manual')),
  check_group   text        NOT NULL,
  check_name    text        NOT NULL,
  check_order   integer     NOT NULL,
  status        text        NOT NULL CHECK (status IN ('pass', 'fail', 'warn', 'skip')),
  value         text,
  expected      text,
  error_message text,
  duration_ms   integer,
  created_at    timestamptz DEFAULT now()
);

-- Composite index: fetch all checks for a run in execution order
CREATE INDEX idx_system_health_run_id ON public.system_health(run_id, check_order);

-- Index: fetch most recent runs for dashboard display
CREATE INDEX idx_system_health_recent ON public.system_health(created_at DESC);

-- Partial index: fast failure/warning lookup (dashboard failure section)
CREATE INDEX idx_system_health_failures ON public.system_health(status, created_at DESC)
  WHERE status IN ('fail', 'warn');

ALTER TABLE public.system_health ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role manages system_health"
  ON public.system_health FOR ALL USING (true) WITH CHECK (true);
