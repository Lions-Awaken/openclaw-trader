-- =============================================================================
-- Adversarial Ensemble Architecture — Schema Foundation
--
-- Implements shadow profile infrastructure for the multi-agent ensemble:
-- three shadow profiles (SKEPTIC, CONTRARIAN, REGIME_WATCHER) run the full
-- 5-tumbler inference chain alongside the live profile without executing
-- trades. Divergences are stored in shadow_divergences for grading by the
-- calibrator, which feeds DWM-weighted fitness scores back into the ensemble.
--
-- Changes:
--   1. strategy_profiles: add shadow metadata columns (is_shadow, shadow_type,
--      fitness_score, dwm_weight, predicted_utility, divergence_rate,
--      conditional_brier, last_graded_at, times_correct, times_dissented)
--   2. inference_chains: add profile_name column + backfill CONGRESS_MIRROR
--   3. shadow_divergences: new table with RLS
--   4. Seed 3 shadow profiles (SKEPTIC, CONTRARIAN, REGIME_WATCHER)
--   5. Expand scan_type CHECKs on inference_chains and signal_evaluations
--      to include shadow_skeptic, shadow_contrarian, shadow_regime_watcher
--
-- Applied: vpollvsbtushbiapoflr — 2026-04-07
-- =============================================================================

-- =============================================================================
-- 1. strategy_profiles — shadow metadata columns
-- =============================================================================

ALTER TABLE public.strategy_profiles
  ADD COLUMN IF NOT EXISTS is_shadow          boolean DEFAULT false,
  ADD COLUMN IF NOT EXISTS shadow_type        text CHECK (
    shadow_type IS NULL OR shadow_type IN ('SKEPTIC', 'CONTRARIAN', 'REGIME_WATCHER')
  ),
  ADD COLUMN IF NOT EXISTS fitness_score      numeric(6,4) DEFAULT 0.0,
  ADD COLUMN IF NOT EXISTS dwm_weight         numeric(6,4) DEFAULT 1.0,
  ADD COLUMN IF NOT EXISTS predicted_utility  numeric(6,4) DEFAULT 0.0,
  ADD COLUMN IF NOT EXISTS divergence_rate    numeric(5,4) DEFAULT 0.0,
  ADD COLUMN IF NOT EXISTS conditional_brier  numeric(6,4),
  ADD COLUMN IF NOT EXISTS last_graded_at     timestamptz,
  ADD COLUMN IF NOT EXISTS times_correct      integer DEFAULT 0,
  ADD COLUMN IF NOT EXISTS times_dissented    integer DEFAULT 0;

-- =============================================================================
-- 2. inference_chains — profile_name column + backfill
-- =============================================================================

ALTER TABLE public.inference_chains
  ADD COLUMN IF NOT EXISTS profile_name text DEFAULT 'UNKNOWN';

-- Backfill: chains written after CONGRESS_MIRROR was deployed (2026-03-30)
-- by the scanner scan_type are attributable to CONGRESS_MIRROR (the active
-- profile at that time).
UPDATE public.inference_chains
SET profile_name = 'CONGRESS_MIRROR'
WHERE scan_type = 'scanner'
  AND created_at >= '2026-03-30T00:00:00Z'
  AND profile_name = 'UNKNOWN';

CREATE INDEX IF NOT EXISTS idx_inference_chains_profile
  ON public.inference_chains(profile_name, chain_date DESC);

-- =============================================================================
-- 3. shadow_divergences — divergence records between live and shadow profiles
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.shadow_divergences (
  id                              uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
  ticker                          text        NOT NULL,
  divergence_date                 date        NOT NULL,

  -- Live profile side
  live_profile                    text        NOT NULL,
  live_decision                   text        NOT NULL,
  live_confidence                 numeric(4,3),
  live_chain_id                   uuid        REFERENCES public.inference_chains(id) ON DELETE SET NULL,

  -- Shadow profile side
  shadow_profile                  text        NOT NULL,
  shadow_type                     text        NOT NULL,
  shadow_decision                 text        NOT NULL,
  shadow_confidence               numeric(4,3),
  shadow_stopping_reason          text,
  shadow_chain_id                 uuid        REFERENCES public.inference_chains(id) ON DELETE SET NULL,

  -- Where the divergence first emerged in the tumbler stack
  first_diverged_at_tumbler       integer,
  tumbler_divergence_vector       jsonb       DEFAULT '{}'::jsonb,

  -- Outcome — populated by calibrator after trade resolves
  trade_executed                  boolean     DEFAULT false,
  actual_outcome                  text,
  actual_pnl                      numeric(10,2),
  shadow_was_right                boolean,
  conditional_brier_contribution  numeric(6,4),
  save_value                      numeric(10,2),   -- estimated P&L saved if shadow was right and live was wrong

  created_at                      timestamptz DEFAULT now()
);

-- Indexes
CREATE INDEX idx_shadow_div_ticker   ON public.shadow_divergences(ticker, divergence_date DESC);
CREATE INDEX idx_shadow_div_profile  ON public.shadow_divergences(shadow_profile, shadow_was_right);
CREATE INDEX idx_shadow_div_ungraded ON public.shadow_divergences(shadow_was_right)
  WHERE shadow_was_right IS NULL;

-- RLS
ALTER TABLE public.shadow_divergences ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role manages shadow_divergences"
  ON public.shadow_divergences FOR ALL USING (true) WITH CHECK (true);

-- =============================================================================
-- 4. Seed 3 shadow profiles
--    All non-essential columns use their column defaults (NULL or the value
--    set by prior migrations). Only columns that differ from defaults are listed.
-- =============================================================================

INSERT INTO public.strategy_profiles (
  profile_name,
  description,
  active,
  min_signal_score,
  min_tumbler_depth,
  min_confidence,
  is_shadow,
  shadow_type,
  auto_execute_all
) VALUES (
  'SKEPTIC',
  'Shadow: conservative devil''s advocate. Requires stronger signal evidence than the live profile before agreeing to enter. Generates labeled data for Conditional Brier scoring.',
  false,
  5,
  4,
  0.70,
  true,
  'SKEPTIC',
  false
)
ON CONFLICT (profile_name) DO NOTHING;

INSERT INTO public.strategy_profiles (
  profile_name,
  description,
  active,
  min_signal_score,
  min_tumbler_depth,
  min_confidence,
  is_shadow,
  shadow_type
) VALUES (
  'CONTRARIAN',
  'Shadow: finds reasons NOT to enter. Low bar for engagement (looks at everything), but biased toward caution and regime risk. Generates labeled data for Regime-Conditional IC scoring.',
  false,
  3,
  2,
  0.45,
  true,
  'CONTRARIAN'
)
ON CONFLICT (profile_name) DO NOTHING;

INSERT INTO public.strategy_profiles (
  profile_name,
  description,
  active,
  min_signal_score,
  min_tumbler_depth,
  min_confidence,
  is_shadow,
  shadow_type,
  bypass_regime_gate
) VALUES (
  'REGIME_WATCHER',
  'Shadow: macro-only observer. Stops at Tumbler 3 (flow + cross-asset). Specialises in regime detection and latency measurement. Generates labeled data for Detection Latency scoring.',
  false,
  1,
  2,
  0.35,
  true,
  'REGIME_WATCHER',
  true
)
ON CONFLICT (profile_name) DO NOTHING;

-- =============================================================================
-- 5. Expand scan_type CHECK constraints to include shadow run types
--
-- Current values as of 20260330_fix_scan_type_constraints.sql:
--   inference_chains:   pre_market, midday, close, catalyst_triggered, manual, scanner
--   signal_evaluations: pre_market, midday, close, catalyst_triggered, manual, scanner
--
-- New values added: unleashed, shadow_skeptic, shadow_contrarian, shadow_regime_watcher
-- (unleashed included for parity with scanner_unleashed.py run mode)
-- =============================================================================

-- inference_chains
ALTER TABLE public.inference_chains
  DROP CONSTRAINT IF EXISTS inference_chains_scan_type_check;

ALTER TABLE public.inference_chains
  ADD CONSTRAINT inference_chains_scan_type_check
  CHECK (scan_type = ANY (ARRAY[
    'pre_market'::text,
    'midday'::text,
    'close'::text,
    'catalyst_triggered'::text,
    'manual'::text,
    'scanner'::text,
    'unleashed'::text,
    'shadow_skeptic'::text,
    'shadow_contrarian'::text,
    'shadow_regime_watcher'::text
  ]));

-- signal_evaluations
ALTER TABLE public.signal_evaluations
  DROP CONSTRAINT IF EXISTS signal_evaluations_scan_type_check;

ALTER TABLE public.signal_evaluations
  ADD CONSTRAINT signal_evaluations_scan_type_check
  CHECK (scan_type = ANY (ARRAY[
    'pre_market'::text,
    'midday'::text,
    'close'::text,
    'catalyst_triggered'::text,
    'manual'::text,
    'scanner'::text,
    'unleashed'::text,
    'shadow_skeptic'::text,
    'shadow_contrarian'::text,
    'shadow_regime_watcher'::text
  ]));
