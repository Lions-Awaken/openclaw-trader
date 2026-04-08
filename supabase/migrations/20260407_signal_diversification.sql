-- =============================================================================
-- Signal Diversification — Options Flow + Form 4 Insider Schema
--
-- Adds two new signal source tables and seeds two new shadow profiles
-- that trade on those signals exclusively. Shadow profiles never execute
-- trades — they generate labeled prediction data for the calibrator.
--
-- Changes:
--   1. options_flow_signals — unusual options activity (sweeps, blocks, darkpool)
--   2. form4_signals — SEC Form 4 insider purchase/sale filings
--   3. Expand shadow_type CHECK to include OPTIONS_FLOW and FORM4_INSIDER
--   4. Expand scan_type CHECKs to include shadow_options_flow, shadow_form4_insider
--   5. Seed OPTIONS_FLOW shadow profile (shadow_type=SKEPTIC, 5-day hold)
--   6. Seed FORM4_INSIDER shadow profile (shadow_type=CONTRARIAN, 15-day hold)
--
-- Applied: vpollvsbtushbiapoflr — 2026-04-07
-- =============================================================================

-- =============================================================================
-- 1. options_flow_signals
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.options_flow_signals (
  id                 uuid           DEFAULT gen_random_uuid() PRIMARY KEY,
  ticker             text           NOT NULL,
  signal_date        date           NOT NULL,
  signal_type        text           NOT NULL CHECK (signal_type IN ('unusual_call', 'unusual_put', 'sweep', 'block', 'darkpool')),
  strike             numeric(10,2),
  expiry             date,
  premium            numeric(12,2),
  open_interest      integer,
  volume             integer,
  implied_volatility numeric(6,4),
  sentiment          text           CHECK (sentiment IN ('bullish', 'bearish', 'neutral')),
  source             text           DEFAULT 'manual',
  raw_data           jsonb          DEFAULT '{}'::jsonb,
  created_at         timestamptz    DEFAULT now()
);

-- Composite index: per-ticker recent signals (most common query pattern)
CREATE INDEX idx_options_flow_ticker ON public.options_flow_signals(ticker, signal_date DESC);

-- Index: recent signals by sentiment (dashboard feed + scanner enrichment)
CREATE INDEX idx_options_flow_recent ON public.options_flow_signals(signal_date DESC, sentiment);

ALTER TABLE public.options_flow_signals ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role manages options_flow_signals"
  ON public.options_flow_signals FOR ALL USING (true) WITH CHECK (true);

-- =============================================================================
-- 2. form4_signals
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.form4_signals (
  id                      uuid           DEFAULT gen_random_uuid() PRIMARY KEY,
  ticker                  text           NOT NULL,
  signal_date             date           NOT NULL,
  filing_date             date           NOT NULL,
  filer_name              text           NOT NULL,
  filer_title             text,
  transaction_type        text           NOT NULL CHECK (transaction_type IN ('purchase', 'sale', 'gift', 'exercise')),
  shares                  integer,
  price_per_share         numeric(10,2),
  total_value             numeric(14,2),
  shares_owned_after      integer,
  ownership_pct_change    numeric(6,4),
  days_since_last_filing  integer,
  cluster_count           integer        DEFAULT 1,
  source                  text           DEFAULT 'sec_edgar',
  raw_data                jsonb          DEFAULT '{}'::jsonb,
  created_at              timestamptz    DEFAULT now()
);

-- Composite index: per-ticker recent filings
CREATE INDEX idx_form4_ticker ON public.form4_signals(ticker, signal_date DESC);

-- Partial index: purchase-only filings (primary alpha signal — sales are noise)
CREATE INDEX idx_form4_purchases ON public.form4_signals(transaction_type, signal_date DESC)
  WHERE transaction_type = 'purchase';

ALTER TABLE public.form4_signals ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role manages form4_signals"
  ON public.form4_signals FOR ALL USING (true) WITH CHECK (true);

-- =============================================================================
-- 3. Expand shadow_type CHECK on strategy_profiles
--
-- Prior constraint (from 20260407_adversarial_ensemble.sql) allowed:
--   SKEPTIC, CONTRARIAN, REGIME_WATCHER
-- New values added: OPTIONS_FLOW, FORM4_INSIDER
-- =============================================================================

ALTER TABLE public.strategy_profiles
  DROP CONSTRAINT IF EXISTS strategy_profiles_shadow_type_check;

ALTER TABLE public.strategy_profiles
  ADD CONSTRAINT strategy_profiles_shadow_type_check
  CHECK (
    shadow_type IS NULL OR shadow_type IN (
      'SKEPTIC',
      'CONTRARIAN',
      'REGIME_WATCHER',
      'OPTIONS_FLOW',
      'FORM4_INSIDER'
    )
  );

-- =============================================================================
-- 4. Expand scan_type CHECKs to include new shadow run types
--
-- Current values (from 20260407_adversarial_ensemble.sql):
--   pre_market, midday, close, catalyst_triggered, manual, scanner,
--   unleashed, shadow_skeptic, shadow_contrarian, shadow_regime_watcher
--
-- New values added: shadow_options_flow, shadow_form4_insider
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
    'shadow_regime_watcher'::text,
    'shadow_options_flow'::text,
    'shadow_form4_insider'::text
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
    'shadow_regime_watcher'::text,
    'shadow_options_flow'::text,
    'shadow_form4_insider'::text
  ]));

-- =============================================================================
-- 5. Seed OPTIONS_FLOW shadow profile
--
-- Trades on unusual options activity: sweeps, blocks, dark pool prints.
-- Alpha decay is fast (1-5 days). Prioritizes speed over depth.
-- shadow_type=SKEPTIC: high-conviction momentum signals only.
-- =============================================================================

INSERT INTO public.strategy_profiles (
  profile_name,
  description,
  active,
  is_shadow,
  shadow_type,
  min_signal_score,
  min_tumbler_depth,
  min_confidence,
  max_risk_per_trade_pct,
  max_concurrent_positions,
  trade_style,
  max_hold_days,
  circuit_breakers_enabled,
  auto_execute_all,
  self_modify_enabled,
  dwm_weight,
  fitness_score
) VALUES (
  'OPTIONS_FLOW',
  'Shadow profile trading on unusual options activity. Sweeps, blocks, and dark pool prints as primary signal. Fast disclosure (same day). Alpha decay: 1-5 days. Never executes — shadow data collection only.',
  false,
  true,
  'SKEPTIC',
  3,
  3,
  0.55,
  1.0,
  0,
  'swing',
  5,
  true,
  false,
  false,
  1.0,
  0.0
) ON CONFLICT (profile_name) DO NOTHING;

-- =============================================================================
-- 6. Seed FORM4_INSIDER shadow profile
--
-- Trades on SEC Form 4 corporate insider purchase filings.
-- Cluster buys and high ownership pct change are the primary signals.
-- Disclosure within 2 business days. Decades of verified academic alpha.
-- shadow_type=CONTRARIAN: patient, slower alpha, 15-day holds.
-- =============================================================================

INSERT INTO public.strategy_profiles (
  profile_name,
  description,
  active,
  is_shadow,
  shadow_type,
  min_signal_score,
  min_tumbler_depth,
  min_confidence,
  max_risk_per_trade_pct,
  max_concurrent_positions,
  trade_style,
  max_hold_days,
  circuit_breakers_enabled,
  auto_execute_all,
  self_modify_enabled,
  dwm_weight,
  fitness_score
) VALUES (
  'FORM4_INSIDER',
  'Shadow profile trading on corporate insider SEC Form 4 purchase filings. Executive cluster buys, high ownership pct change. Disclosure within 2 business days of trade. Decades of verified academic alpha. Never executes — shadow data collection only.',
  false,
  true,
  'CONTRARIAN',
  3,
  3,
  0.55,
  1.0,
  0,
  'swing',
  15,
  true,
  false,
  false,
  1.0,
  0.0
) ON CONFLICT (profile_name) DO NOTHING;
