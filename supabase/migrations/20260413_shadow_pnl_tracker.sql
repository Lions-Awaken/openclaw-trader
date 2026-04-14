-- =============================================================================
-- Shadow P&L Tracker — shadow_positions + shadow_performance tables
--
-- Two new tables that underpin the Shadow P&L tracking feature:
--
--   shadow_positions:    one row per shadow-profile × ticker × entry_date.
--                        Opened by shadow_position_opener.py after each scanner
--                        run. Marked-to-market nightly by shadow_mark_to_market.py.
--                        Exit rules: 10-day time stop, -7.5% stop_loss,
--                        +15%/+25% profit targets, trailing stop.
--
--   shadow_performance:  weekly rollup aggregated by shadow_performance_rollup.py.
--                        One row per (shadow_profile, week_start). UNIQUE constraint
--                        allows safe UPSERT on repeated rollup runs.
--
-- Also fixes two mislabeled shadow_type values in strategy_profiles:
--   OPTIONS_FLOW  was seeded with shadow_type='SKEPTIC'    → fixed to 'OPTIONS_FLOW'
--   FORM4_INSIDER was seeded with shadow_type='CONTRARIAN' → fixed to 'FORM4_INSIDER'
-- The CHECK constraint already permits both values (added in 20260407_kronos_technicals_shadow_profile.sql).
--
-- Applied: vpollvsbtushbiapoflr — 2026-04-13
-- =============================================================================


-- =============================================================================
-- 1. shadow_positions
-- =============================================================================

CREATE TABLE public.shadow_positions (
    id                   uuid           PRIMARY KEY DEFAULT gen_random_uuid(),
    shadow_profile       text           NOT NULL,
    ticker               text           NOT NULL,
    entry_date           date           NOT NULL,
    entry_price          numeric        NOT NULL,
    position_size_usd    numeric        NOT NULL DEFAULT 10000,
    position_size_shares numeric,

    -- Links back to the inference chain and divergence record that triggered this position
    shadow_chain_id      uuid           REFERENCES public.inference_chains(id) ON DELETE SET NULL,
    shadow_divergence_id uuid           REFERENCES public.shadow_divergences(id) ON DELETE SET NULL,

    -- Divergence context vs the live profile's decision on the same ticker+date
    was_divergent        boolean        DEFAULT false,
    vs_live_decision     text,

    -- Live mark-to-market state (updated nightly by shadow_mark_to_market.py)
    current_price        numeric,
    current_pnl          numeric,
    current_pnl_pct      numeric,
    peak_pnl_pct         numeric        DEFAULT 0,

    -- Lifecycle
    status               text           NOT NULL DEFAULT 'open'
                                        CHECK (status IN ('open', 'closed', 'stopped', 'expired')),

    -- Exit fields (populated when status changes from 'open')
    exit_date            date,
    exit_price           numeric,
    final_pnl            numeric,
    final_pnl_pct        numeric,
    close_reason         text           CHECK (close_reason IN (
                                            'time_stop',
                                            'profit_target_1',
                                            'profit_target_2',
                                            'trailing_stop',
                                            'stop_loss',
                                            'manual'
                                        )),
    shadow_was_right     boolean,       -- true if final_pnl > 0

    created_at           timestamptz    NOT NULL DEFAULT now()
);

-- Foreign-key index (shadow_chain_id)
CREATE INDEX idx_shadow_positions_chain_id
    ON public.shadow_positions(shadow_chain_id);

-- Foreign-key index (shadow_divergence_id)
CREATE INDEX idx_shadow_positions_divergence_id
    ON public.shadow_positions(shadow_divergence_id);

-- High-cardinality WHERE: filter by profile in dashboard + rollup queries
CREATE INDEX idx_shadow_positions_profile
    ON public.shadow_positions(shadow_profile);

-- Composite: prevents duplicate open positions per profile+ticker+date (app logic),
-- also used for the EXISTS check in shadow_position_opener.py
CREATE INDEX idx_shadow_positions_ticker_date
    ON public.shadow_positions(ticker, entry_date);

-- Status filter: open-position queries in mark-to-market and dashboard
CREATE INDEX idx_shadow_positions_status
    ON public.shadow_positions(status);

ALTER TABLE public.shadow_positions ENABLE ROW LEVEL SECURITY;

-- Service role only — shadow positions are written and read exclusively by
-- backend scripts; no user-facing read access is needed.
CREATE POLICY "service_role_manages_shadow_positions"
    ON public.shadow_positions
    FOR ALL
    USING (true)
    WITH CHECK (true);


-- =============================================================================
-- 2. shadow_performance
-- =============================================================================

CREATE TABLE public.shadow_performance (
    id                    uuid           PRIMARY KEY DEFAULT gen_random_uuid(),
    shadow_profile        text           NOT NULL,
    week_start            date           NOT NULL,

    -- Trade counts for the week
    trades_opened         integer        DEFAULT 0,
    trades_closed         integer        DEFAULT 0,
    trades_won            integer        DEFAULT 0,
    trades_lost           integer        DEFAULT 0,

    -- Aggregate metrics
    win_rate_pct          numeric,
    total_pnl             numeric        DEFAULT 0,
    avg_pnl_per_trade     numeric,
    best_trade_pnl        numeric,
    worst_trade_pnl       numeric,

    -- Divergence-specific metrics
    divergent_trades      integer        DEFAULT 0,
    divergent_win_rate    numeric,

    -- Live profile comparison for the same calendar week
    live_pnl_same_period  numeric,
    vs_live_delta         numeric,

    -- DWM weight at the start and end of the week (from calibrator)
    dwm_weight_start      numeric,
    dwm_weight_end        numeric,

    created_at            timestamptz    NOT NULL DEFAULT now(),

    -- One row per agent per week — safe UPSERT key for rollup script
    UNIQUE (shadow_profile, week_start)
);

ALTER TABLE public.shadow_performance ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_manages_shadow_performance"
    ON public.shadow_performance
    FOR ALL
    USING (true)
    WITH CHECK (true);


-- =============================================================================
-- 3. Data fix — correct mislabeled shadow_type values in strategy_profiles
--
-- OPTIONS_FLOW  was seeded with shadow_type='SKEPTIC'    → should be 'OPTIONS_FLOW'
-- FORM4_INSIDER was seeded with shadow_type='CONTRARIAN' → should be 'FORM4_INSIDER'
--
-- Both target values are already permitted by the CHECK constraint added in
-- 20260407_kronos_technicals_shadow_profile.sql.
-- =============================================================================

UPDATE public.strategy_profiles
    SET shadow_type = 'FORM4_INSIDER'
    WHERE profile_name = 'FORM4_INSIDER';

UPDATE public.strategy_profiles
    SET shadow_type = 'OPTIONS_FLOW'
    WHERE profile_name = 'OPTIONS_FLOW';
