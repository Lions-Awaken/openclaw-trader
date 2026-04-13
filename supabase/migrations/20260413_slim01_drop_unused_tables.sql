-- SLIM-01: Drop 4 unused tables — tuning_profiles, tuning_telemetry, regime_log, stack_heartbeats
--
-- These tables have zero active reads from any production script. They were
-- created during early infrastructure exploration but the tuning system was
-- never operationalized. heartbeat.py writes to stack_heartbeats but nothing
-- reads it; regime_log has no writer or reader in production.
--
-- Also removes:
--   - pg_cron job: purge-old-tuning-telemetry (retention job for tuning_telemetry)
--   - view: tuning_profile_performance (depends on tuning_profiles + tuning_telemetry)
--   - FK column: pipeline_runs.tuning_profile_id (references tuning_profiles)

-- ============================================================================
-- Step 1: Remove pg_cron retention job for tuning_telemetry
-- Job name confirmed on live DB: 'purge-tuning-telemetry' (jobid=7)
-- ============================================================================

SELECT cron.unschedule('purge-tuning-telemetry');

-- ============================================================================
-- Step 2: Check and remove any other cron jobs referencing these tables
-- (idempotent — unschedule returns false if job doesn't exist, no error)
-- ============================================================================

DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT jobname FROM cron.job
    WHERE command ILIKE '%tuning_profiles%'
       OR command ILIKE '%tuning_telemetry%'
       OR command ILIKE '%regime_log%'
       OR command ILIKE '%stack_heartbeats%'
  LOOP
    PERFORM cron.unschedule(r.jobname);
    RAISE NOTICE 'Unscheduled cron job: %', r.jobname;
  END LOOP;
END;
$$;

-- ============================================================================
-- Step 3: Drop dependent view
-- ============================================================================

DROP VIEW IF EXISTS public.tuning_profile_performance;

-- ============================================================================
-- Step 4: Drop FK column on pipeline_runs that references tuning_profiles
-- ============================================================================

ALTER TABLE public.pipeline_runs DROP COLUMN IF EXISTS tuning_profile_id;

-- ============================================================================
-- Step 5: Drop tuning_telemetry (FK references tuning_profiles)
-- ============================================================================

DROP TABLE IF EXISTS public.tuning_telemetry CASCADE;

-- ============================================================================
-- Step 6: Drop tuning_profiles
-- ============================================================================

DROP TABLE IF EXISTS public.tuning_profiles CASCADE;

-- ============================================================================
-- Step 7: Drop regime_log
-- ============================================================================

DROP TABLE IF EXISTS public.regime_log CASCADE;

-- ============================================================================
-- Step 8: Drop stack_heartbeats
-- ============================================================================

DROP TABLE IF EXISTS public.stack_heartbeats CASCADE;

-- ============================================================================
-- Verification
-- Expected result: 0 rows
-- SELECT tablename FROM pg_tables
--   WHERE schemaname = 'public'
--     AND tablename IN ('tuning_profiles', 'tuning_telemetry', 'regime_log', 'stack_heartbeats');
-- ============================================================================
