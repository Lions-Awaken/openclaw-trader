-- Migration: Add KRONOS_TECHNICALS shadow profile
-- Adds the Kronos financial time series foundation model shadow agent.
-- OHLCV-only, 50 Monte Carlo paths, 15-bar horizon, T1+T2 depth only.

-- 1. Expand shadow_type constraint on strategy_profiles
ALTER TABLE public.strategy_profiles DROP CONSTRAINT IF EXISTS strategy_profiles_shadow_type_check;
ALTER TABLE public.strategy_profiles ADD CONSTRAINT strategy_profiles_shadow_type_check
  CHECK (shadow_type IN (
    'SKEPTIC', 'CONTRARIAN', 'REGIME_WATCHER',
    'OPTIONS_FLOW', 'FORM4_INSIDER', 'KRONOS_TECHNICALS'
  ));

-- 2. Expand scan_type constraint on inference_chains
ALTER TABLE public.inference_chains DROP CONSTRAINT IF EXISTS inference_chains_scan_type_check;
ALTER TABLE public.inference_chains ADD CONSTRAINT inference_chains_scan_type_check
  CHECK (scan_type = ANY (ARRAY[
    'pre_market'::text, 'midday'::text, 'close'::text,
    'catalyst_triggered'::text, 'manual'::text, 'scanner'::text,
    'unleashed'::text, 'shadow_skeptic'::text, 'shadow_contrarian'::text,
    'shadow_regime_watcher'::text, 'shadow_options_flow'::text,
    'shadow_form4_insider'::text, 'shadow_kronos_technicals'::text
  ]));

-- 3. Expand scan_type constraint on signal_evaluations
ALTER TABLE public.signal_evaluations DROP CONSTRAINT IF EXISTS signal_evaluations_scan_type_check;
ALTER TABLE public.signal_evaluations ADD CONSTRAINT signal_evaluations_scan_type_check
  CHECK (scan_type = ANY (ARRAY[
    'pre_market'::text, 'midday'::text, 'close'::text,
    'catalyst_triggered'::text, 'manual'::text, 'scanner'::text,
    'unleashed'::text, 'shadow_skeptic'::text, 'shadow_contrarian'::text,
    'shadow_regime_watcher'::text, 'shadow_options_flow'::text,
    'shadow_form4_insider'::text, 'shadow_kronos_technicals'::text
  ]));

-- 4. Seed the KRONOS_TECHNICALS shadow profile row
INSERT INTO public.strategy_profiles (
  profile_name, description, active, is_shadow, shadow_type,
  min_signal_score, min_tumbler_depth, min_confidence,
  max_risk_per_trade_pct, max_concurrent_positions,
  trade_style, max_hold_days, circuit_breakers_enabled,
  auto_execute_all, self_modify_enabled, dwm_weight, fitness_score
) VALUES (
  'KRONOS_TECHNICALS',
  'Pure price pattern shadow agent using Kronos financial time series foundation model. OHLCV only — no news, filings, or fundamentals. 50 Monte Carlo paths, 15-bar horizon, directional accuracy grading. Never executes trades.',
  false, true, 'KRONOS_TECHNICALS',
  2, 2, 0.50,
  1.0, 0, 'swing', 10, true,
  false, false, 1.0, 0.0
) ON CONFLICT (profile_name) DO NOTHING;
