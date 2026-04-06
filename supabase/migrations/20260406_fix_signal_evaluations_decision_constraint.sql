-- =============================================================================
-- Fix: signal_evaluations.decision CHECK constraint missing 'strong_enter'
--
-- Root cause: inference_engine.decide() returns 'strong_enter' when final
-- confidence >= DECISION_THRESHOLDS["strong_enter"] (default 0.75). scanner.py
-- passes inf_result["final_decision"] directly to tracer.log_signal_evaluation()
-- as the `decision` column. The CHECK constraint only allowed 4 values:
--   ('enter', 'skip', 'watch', 'veto')
-- Missing 'strong_enter' causes a CHECK constraint violation for every high-
-- confidence ticker, silently dropping the signal_evaluation row (buffered to
-- tracer_buffer.jsonl on ridley).
--
-- inference_chains.final_decision already allows 'strong_enter' — this brings
-- signal_evaluations into parity.
--
-- Applied: vpollvsbtushbiapoflr — 2026-04-06
-- =============================================================================

ALTER TABLE public.signal_evaluations
  DROP CONSTRAINT IF EXISTS signal_evaluations_decision_check;

ALTER TABLE public.signal_evaluations
  ADD CONSTRAINT signal_evaluations_decision_check
  CHECK (decision = ANY (ARRAY[
    'strong_enter'::text,
    'enter'::text,
    'watch'::text,
    'skip'::text,
    'veto'::text
  ]));
