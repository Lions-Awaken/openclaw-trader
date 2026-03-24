-- ============================================================================
-- CLEANUP: Remove openclaw-trader tables from Twilight Underground database
-- Target: uupmzaglafeiakamefit (Twilight Underground)
--
-- These tables were accidentally migrated to TU but all data lives in the
-- dedicated openclaw-trader Supabase (vpollvsbtushbiapoflr).
--
-- SAFETY: This script checks row counts before dropping. If any table has
-- data, the transaction is aborted.
--
-- ROLLBACK: Use 02_rollback_tu_database.sql to recreate everything.
-- ============================================================================

BEGIN;

-- ============================================================================
-- SAFETY CHECKS: Abort if any table has data
-- ============================================================================

DO $$
DECLARE
  _count bigint;
  _table text;
  _tables text[] := ARRAY[
    'tuning_telemetry', 'tuning_profiles', 'confidence_calibration',
    'cost_ledger', 'inference_chains', 'pattern_templates', 'catalyst_events',
    'strategy_adjustments', 'meta_reflections', 'signal_evaluations',
    'data_quality_checks', 'order_events', 'pipeline_runs',
    'budget_config', 'stack_heartbeats', 'magic_link_tokens', 'strategy_profiles'
  ];
BEGIN
  FOREACH _table IN ARRAY _tables LOOP
    -- Check if table exists first
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = _table) THEN
      EXECUTE format('SELECT count(*) FROM public.%I', _table) INTO _count;
      IF _count > 0 THEN
        RAISE EXCEPTION 'SAFETY ABORT: Table % has % rows. Expected 0. Aborting cleanup.', _table, _count;
      END IF;
    END IF;
  END LOOP;
  RAISE NOTICE 'All safety checks passed. All openclaw tables in TU have 0 rows.';
END $$;

-- ============================================================================
-- Step 1: Remove pg_cron jobs (must be done before dropping tables)
-- ============================================================================

SELECT cron.unschedule('purge-old-pipeline-runs');
SELECT cron.unschedule('purge-old-data-quality-checks');
SELECT cron.unschedule('purge-old-order-events');
SELECT cron.unschedule('purge-old-catalyst-events');
SELECT cron.unschedule('purge-old-inference-chains');
SELECT cron.unschedule('purge-old-cost-ledger');
SELECT cron.unschedule('purge-old-confidence-calibration');
SELECT cron.unschedule('purge-old-tuning-telemetry');
SELECT cron.unschedule('purge-expired-magic-links');

-- ============================================================================
-- Step 2: Drop RAG functions
-- ============================================================================

DROP FUNCTION IF EXISTS public.match_signal_evaluations(extensions.vector, float, int);
DROP FUNCTION IF EXISTS public.match_meta_reflections(extensions.vector, float, int);
DROP FUNCTION IF EXISTS public.match_catalyst_events(extensions.vector, float, int, text, text);
DROP FUNCTION IF EXISTS public.match_inference_chains(extensions.vector, float, int);
DROP FUNCTION IF EXISTS public.match_pattern_templates(extensions.vector, float, int);

-- ============================================================================
-- Step 3: Drop views (must be done before dropping tables they reference)
-- ============================================================================

DROP VIEW IF EXISTS public.signal_accuracy_report;
DROP VIEW IF EXISTS public.tuning_profile_performance;

-- ============================================================================
-- Step 4: Drop tables in dependency order (children first)
-- ============================================================================

-- Tuning system (depends on pipeline_runs, tuning_profiles)
DROP TABLE IF EXISTS public.tuning_telemetry CASCADE;

-- Remove tuning FK from pipeline_runs before dropping tuning_profiles
ALTER TABLE public.pipeline_runs DROP COLUMN IF EXISTS tuning_profile_id;
DROP TABLE IF EXISTS public.tuning_profiles CASCADE;

-- Tumbler architecture (depends on pipeline_runs, signal_evaluations, pattern_templates)
DROP TABLE IF EXISTS public.confidence_calibration CASCADE;
DROP TABLE IF EXISTS public.cost_ledger CASCADE;
DROP TABLE IF EXISTS public.inference_chains CASCADE;
DROP TABLE IF EXISTS public.catalyst_events CASCADE;
DROP TABLE IF EXISTS public.pattern_templates CASCADE;
DROP TABLE IF EXISTS public.budget_config CASCADE;

-- Meta-learning (depends on pipeline_runs, meta_reflections)
DROP TABLE IF EXISTS public.strategy_adjustments CASCADE;
DROP TABLE IF EXISTS public.meta_reflections CASCADE;

-- Signal tracing (depends on pipeline_runs)
DROP TABLE IF EXISTS public.signal_evaluations CASCADE;

-- Observability foundation
DROP TABLE IF EXISTS public.data_quality_checks CASCADE;
DROP TABLE IF EXISTS public.order_events CASCADE;
DROP TABLE IF EXISTS public.pipeline_runs CASCADE;

-- Dashboard-specific tables
DROP TABLE IF EXISTS public.stack_heartbeats CASCADE;
DROP TABLE IF EXISTS public.magic_link_tokens CASCADE;
DROP TABLE IF EXISTS public.strategy_profiles CASCADE;

COMMIT;

-- ============================================================================
-- Verify cleanup
-- ============================================================================
DO $$
DECLARE
  _remaining int;
BEGIN
  SELECT count(*) INTO _remaining
  FROM information_schema.tables
  WHERE table_schema = 'public'
    AND table_name IN (
      'pipeline_runs', 'order_events', 'data_quality_checks',
      'signal_evaluations', 'meta_reflections', 'strategy_adjustments',
      'catalyst_events', 'pattern_templates', 'inference_chains',
      'cost_ledger', 'budget_config', 'confidence_calibration',
      'tuning_profiles', 'tuning_telemetry',
      'stack_heartbeats', 'magic_link_tokens', 'strategy_profiles'
    );
  IF _remaining = 0 THEN
    RAISE NOTICE 'SUCCESS: All % openclaw tables removed from TU database.', 17;
  ELSE
    RAISE WARNING 'PARTIAL: % openclaw tables still remain in TU database.', _remaining;
  END IF;
END $$;
