-- =============================================================================
-- Trade Learnings: Post-trade RAG ingestion pipeline
-- Triggered every time a trade closes. Stores structured post-mortems
-- with embeddings so future inference chains can learn from past outcomes.
--
-- Applied on: vpollvsbtushbiapoflr (OpenClaw Trader project)
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Table: trade_learnings
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trade_learnings (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticker                  text NOT NULL,
    trade_date              date NOT NULL,
    entry_price             numeric(12,4),
    exit_price              numeric(12,4),
    pnl                     numeric(12,4),
    pnl_pct                 numeric(6,3),
    outcome                 text CHECK (outcome IN ('STRONG_WIN','WIN','SCRATCH','LOSS','STRONG_LOSS')),
    hold_days               integer,

    -- What we expected going in
    expected_direction      text CHECK (expected_direction IN ('bullish','bearish','neutral')),
    expected_confidence     numeric(5,4),
    expected_catalysts      text[],
    expected_target_pct     numeric(6,3),

    -- What actually happened
    actual_direction        text CHECK (actual_direction IN ('bullish','bearish','flat')),
    actual_move_pct         numeric(6,3),
    expectation_accuracy    text CHECK (expectation_accuracy IN ('met','exceeded','missed','opposite')),

    -- Context chain (links back to the inference that generated this trade)
    inference_chain_id      uuid REFERENCES inference_chains(id) ON DELETE SET NULL,
    signal_score            integer,
    tumbler_depth           integer,
    stopping_reason         text,

    -- Where did we get it right / wrong
    catalyst_match          text,       -- did the expected catalysts materialize?
    pattern_effectiveness   text,       -- did matched patterns hold up?
    key_variance            text,       -- the delta between expectation and reality

    -- Structured Claude post-mortem
    what_worked             text,
    what_failed             text,
    key_lesson              text,

    -- Contextual snapshots
    setup_conditions        jsonb,      -- technical + fundamental at entry
    exit_conditions         jsonb,      -- what triggered the exit
    market_context          jsonb,      -- SPY/QQQ movement during hold period
    active_catalysts        jsonb,      -- catalysts that were active during hold

    -- RAG vector
    content                 text,       -- full text for embedding
    embedding               vector(768),
    metadata                jsonb DEFAULT '{}',
    pipeline_run_id         uuid,
    created_at              timestamptz DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_trade_learnings_ticker      ON trade_learnings(ticker);
CREATE INDEX IF NOT EXISTS idx_trade_learnings_trade_date  ON trade_learnings(trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_trade_learnings_outcome     ON trade_learnings(outcome);
CREATE INDEX IF NOT EXISTS idx_trade_learnings_created_at  ON trade_learnings(created_at DESC);

-- HNSW vector index for fast similarity search
CREATE INDEX IF NOT EXISTS idx_trade_learnings_embedding
    ON trade_learnings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ---------------------------------------------------------------------------
-- RAG function: match_trade_learnings
-- Returns similar past trade post-mortems given a query embedding.
-- Blended ranking: 70% similarity + 30% recency (within last 90 days gets bonus).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION match_trade_learnings(
    query_embedding   vector(768),
    match_threshold   float    DEFAULT 0.50,
    match_count       int      DEFAULT 5,
    filter_ticker     text     DEFAULT NULL
)
RETURNS TABLE (
    id                   uuid,
    ticker               text,
    trade_date           date,
    outcome              text,
    pnl                  numeric,
    pnl_pct              numeric,
    expected_confidence  numeric,
    actual_move_pct      numeric,
    expectation_accuracy text,
    key_lesson           text,
    what_worked          text,
    what_failed          text,
    key_variance         text,
    catalyst_match       text,
    similarity           float
)
LANGUAGE sql STABLE
AS $$
    SELECT
        tl.id,
        tl.ticker,
        tl.trade_date,
        tl.outcome,
        tl.pnl,
        tl.pnl_pct,
        tl.expected_confidence,
        tl.actual_move_pct,
        tl.expectation_accuracy,
        tl.key_lesson,
        tl.what_worked,
        tl.what_failed,
        tl.key_variance,
        tl.catalyst_match,
        -- Blended score: 70% cosine similarity + 30% recency bonus
        (0.7 * (1 - (tl.embedding <=> query_embedding))) +
        (0.3 * GREATEST(0, 1 - EXTRACT(DAY FROM (now() - tl.created_at)) / 90.0)) AS similarity
    FROM trade_learnings tl
    WHERE
        tl.embedding IS NOT NULL
        AND 1 - (tl.embedding <=> query_embedding) > match_threshold
        AND (filter_ticker IS NULL OR tl.ticker = filter_ticker)
    ORDER BY similarity DESC
    LIMIT match_count;
$$;

-- ---------------------------------------------------------------------------
-- Also wire trade outcomes back to inference_chains when a trade closes.
-- This lets the calibrator.py find ungraded chains easily.
-- We update inference_chains.actual_outcome and actual_pnl.
-- (The post_trade_analysis.py script does this via PATCH, no trigger needed.)
-- ---------------------------------------------------------------------------

-- RLS: service role has full access
ALTER TABLE trade_learnings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access" ON trade_learnings
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- 180-day retention (pg_cron job mirrors order_events pattern)
SELECT cron.schedule(
    'purge_trade_learnings',
    '0 3 * * 0',  -- Sunday 3 AM
    $$DELETE FROM trade_learnings WHERE created_at < now() - interval '180 days'$$
);
