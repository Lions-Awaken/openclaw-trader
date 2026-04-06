-- =============================================================================
-- Fix: inference_chains.stopping_reason CHECK constraint
--
-- Root cause: inference_engine.py check_stopping_rule() for the CONGRESS_MIRROR
--   profile (line ~810) can return 'congress_signal_stale' when the disclosure
--   is too old or the freshness score falls below the configured threshold.
--   The existing CHECK constraint only allows 8 values and does not include
--   this new stopping reason, so every CONGRESS_MIRROR tumbler run that hits
--   the staleness check fails at the DB layer with a CHECK violation.
--
-- Fix: Drop the existing constraint and replace it with the full 9-value set,
--   preserving all original values and adding 'congress_signal_stale'.
--   Uses the ARRAY pattern consistent with other recent fix migrations.
--
-- Original values (from 20260321_tumbler_architecture.sql):
--   all_tumblers_clear, confidence_floor, forced_connection,
--   conflicting_signals, veto_signal, insufficient_data,
--   resource_limit, time_limit
--
-- Applied: vpollvsbtushbiapoflr — 2026-04-06
-- =============================================================================

ALTER TABLE public.inference_chains
  DROP CONSTRAINT IF EXISTS inference_chains_stopping_reason_check;

ALTER TABLE public.inference_chains
  ADD CONSTRAINT inference_chains_stopping_reason_check
  CHECK (stopping_reason IS NULL OR stopping_reason = ANY (ARRAY[
    'all_tumblers_clear',
    'confidence_floor',
    'forced_connection',
    'conflicting_signals',
    'veto_signal',
    'insufficient_data',
    'resource_limit',
    'time_limit',
    'congress_signal_stale'
  ]));
