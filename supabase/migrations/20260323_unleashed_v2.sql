-- =============================================================================
-- Unleashed V2: Professional Day Trader Profile
-- Adds bypass_regime_gate + max_hold_days + auto_execute_all to strategy_profiles.
-- Updates UNLEASHED to truly aggressive day-trader settings.
-- Applied on: vpollvsbtushbiapoflr
-- =============================================================================

ALTER TABLE strategy_profiles
    ADD COLUMN IF NOT EXISTS bypass_regime_gate        boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS max_hold_days             integer DEFAULT 10,
    ADD COLUMN IF NOT EXISTS auto_execute_all          boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS scan_all_regimes          boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS relative_strength_signal  boolean DEFAULT false;

-- Update CONSERVATIVE to be explicit about its safe defaults
UPDATE strategy_profiles
SET
    bypass_regime_gate        = false,
    max_hold_days             = 10,
    auto_execute_all          = false,
    scan_all_regimes          = false,
    relative_strength_signal  = false
WHERE profile_name = 'CONSERVATIVE';

-- UNLEASHED: professional day trader — trades every day, in any regime,
-- lives on the profits, generates maximum data for machine learning.
UPDATE strategy_profiles
SET
    description               = 'Professional day trader. No regime gate. No circuit breakers. Auto-executes all signals. Maximum data generation for ML tuning. Paper mode only.',
    annual_target_pct         = 200,
    daily_target_pct          = 0.8,
    weekly_target_pct         = 4.0,
    min_signal_score          = 2,
    min_tumbler_depth         = 1,
    min_confidence            = 0.30,
    max_risk_per_trade_pct    = 15,
    max_concurrent_positions  = 5,
    max_portfolio_risk_pct    = 75,
    position_size_method      = 'aggressive_kelly',
    trade_style               = 'day_trade',
    max_hold_days             = 3,
    circuit_breakers_enabled  = false,
    self_modify_enabled       = true,
    self_modify_requires_approval = false,
    prefer_high_beta          = true,
    bypass_regime_gate        = true,
    auto_execute_all          = true,
    scan_all_regimes          = true,
    relative_strength_signal  = true,
    updated_at                = now()
WHERE profile_name = 'UNLEASHED';
