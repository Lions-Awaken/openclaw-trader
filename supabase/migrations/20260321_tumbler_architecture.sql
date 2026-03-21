-- Tumbler Architecture: Lock & Tumbler Multi-Layered Meta-Analysis
-- 6 new tables: catalyst_events, inference_chains, pattern_templates,
--               cost_ledger, budget_config, confidence_calibration
-- 3 new RAG functions + retention policies + RLS

-- ============================================================================
-- Table 1: catalyst_events — Structured market-moving events with RAG
-- ============================================================================

CREATE TABLE public.catalyst_events (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  ticker text,  -- NULL for macro events
  catalyst_type text NOT NULL
    CHECK (catalyst_type IN (
      'earnings_surprise', 'analyst_action', 'insider_transaction',
      'congressional_trade', 'sec_filing', 'executive_social',
      'influencer_endorsement', 'government_contract', 'product_launch',
      'regulatory_action', 'macro_event', 'sector_rotation',
      'supply_chain', 'partnership', 'other'
    )),
  headline text NOT NULL,
  source text NOT NULL
    CHECK (source IN ('finnhub', 'perplexity', 'sec_edgar', 'quiverquant', 'manual')),
  source_url text,
  event_time timestamptz NOT NULL DEFAULT now(),
  magnitude text NOT NULL DEFAULT 'medium'
    CHECK (magnitude IN ('minor', 'medium', 'major', 'extreme')),
  direction text NOT NULL DEFAULT 'neutral'
    CHECK (direction IN ('bullish', 'bearish', 'neutral', 'ambiguous')),
  sentiment_score numeric(4,3) CHECK (sentiment_score BETWEEN -1.0 AND 1.0),
  affected_tickers text[] DEFAULT '{}',
  sector text,
  price_at_event numeric(12,4),
  price_1d_after numeric(12,4),
  price_5d_after numeric(12,4),
  actual_impact_pct numeric(6,3),
  pattern_template_id uuid,  -- FK added after pattern_templates created
  content text,  -- full text for embedding
  embedding extensions.vector(768),
  metadata jsonb DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_catalyst_events_ticker ON public.catalyst_events(ticker, event_time DESC);
CREATE INDEX idx_catalyst_events_type ON public.catalyst_events(catalyst_type, event_time DESC);
CREATE INDEX idx_catalyst_events_time ON public.catalyst_events(event_time DESC);
CREATE INDEX idx_catalyst_events_direction ON public.catalyst_events(direction, event_time DESC);
CREATE INDEX idx_catalyst_events_magnitude ON public.catalyst_events(magnitude, event_time DESC);

CREATE INDEX idx_catalyst_events_embedding ON public.catalyst_events
  USING hnsw (embedding extensions.vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- ============================================================================
-- Table 2: pattern_templates — Reusable catalyst-response patterns
-- ============================================================================

CREATE TABLE public.pattern_templates (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  pattern_name text UNIQUE NOT NULL,
  pattern_description text NOT NULL,
  pattern_category text NOT NULL
    CHECK (pattern_category IN (
      'catalyst_response', 'signal_combination', 'regime_transition',
      'seasonal', 'sector_sympathy', 'mean_reversion', 'momentum_continuation'
    )),
  trigger_conditions jsonb NOT NULL DEFAULT '{}'::jsonb,
  times_matched integer NOT NULL DEFAULT 0,
  times_correct integer NOT NULL DEFAULT 0,
  success_rate numeric(5,2) GENERATED ALWAYS AS (
    CASE WHEN times_matched > 0
      THEN ROUND(times_correct::numeric / times_matched * 100, 2)
      ELSE 0
    END
  ) STORED,
  avg_return_pct numeric(6,3) DEFAULT 0,
  template_confidence numeric(5,4) DEFAULT 0,
  min_occurrences_for_trust integer NOT NULL DEFAULT 3,
  version integer NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'probation', 'retired', 'superseded')),
  embedding extensions.vector(768),
  last_matched_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_pattern_templates_category ON public.pattern_templates(pattern_category, status);
CREATE INDEX idx_pattern_templates_status ON public.pattern_templates(status, success_rate DESC);

CREATE INDEX idx_pattern_templates_embedding ON public.pattern_templates
  USING hnsw (embedding extensions.vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- Add FK from catalyst_events to pattern_templates
ALTER TABLE public.catalyst_events
  ADD CONSTRAINT fk_catalyst_pattern_template
  FOREIGN KEY (pattern_template_id) REFERENCES public.pattern_templates(id) ON DELETE SET NULL;

-- ============================================================================
-- Table 3: inference_chains — Tumbler-by-tumbler execution log
-- ============================================================================

CREATE TABLE public.inference_chains (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  ticker text NOT NULL,
  chain_date date NOT NULL DEFAULT CURRENT_DATE,
  scan_type text NOT NULL DEFAULT 'pre_market'
    CHECK (scan_type IN ('pre_market', 'midday', 'close', 'catalyst_triggered', 'manual')),
  max_depth_reached integer NOT NULL DEFAULT 0 CHECK (max_depth_reached BETWEEN 0 AND 5),
  final_confidence numeric(5,4) DEFAULT 0 CHECK (final_confidence BETWEEN 0 AND 1),
  final_decision text NOT NULL DEFAULT 'skip'
    CHECK (final_decision IN ('strong_enter', 'enter', 'watch', 'skip', 'veto')),
  stopping_reason text
    CHECK (stopping_reason IS NULL OR stopping_reason IN (
      'all_tumblers_clear', 'confidence_floor', 'forced_connection',
      'conflicting_signals', 'veto_signal', 'insufficient_data',
      'resource_limit', 'time_limit'
    )),
  tumblers jsonb NOT NULL DEFAULT '[]'::jsonb,
  signal_evaluation_id uuid REFERENCES public.signal_evaluations(id) ON DELETE SET NULL,
  catalyst_event_ids uuid[] DEFAULT '{}',
  pattern_template_ids uuid[] DEFAULT '{}',
  actual_outcome text
    CHECK (actual_outcome IS NULL OR actual_outcome IN (
      'STRONG_WIN', 'WIN', 'SCRATCH', 'LOSS', 'STRONG_LOSS'
    )),
  actual_pnl numeric(12,4),
  reasoning_summary text,
  embedding extensions.vector(768),
  pipeline_run_id uuid REFERENCES public.pipeline_runs(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_inference_chains_ticker ON public.inference_chains(ticker, chain_date DESC);
CREATE INDEX idx_inference_chains_date ON public.inference_chains(chain_date DESC);
CREATE INDEX idx_inference_chains_decision ON public.inference_chains(final_decision, chain_date DESC);
CREATE INDEX idx_inference_chains_depth ON public.inference_chains(max_depth_reached, chain_date DESC);
CREATE INDEX idx_inference_chains_signal ON public.inference_chains(signal_evaluation_id);

CREATE INDEX idx_inference_chains_embedding ON public.inference_chains
  USING hnsw (embedding extensions.vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- ============================================================================
-- Table 4: cost_ledger — Every cost and all trading P&L
-- ============================================================================

CREATE TABLE public.cost_ledger (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  ledger_date date NOT NULL DEFAULT CURRENT_DATE,
  category text NOT NULL
    CHECK (category IN (
      'claude_api', 'perplexity_api', 'finnhub_api',
      'fly_hosting', 'supabase', 'ollama_power', 'trade_pnl'
    )),
  subcategory text,
  amount numeric(12,4) NOT NULL,  -- negative=cost, positive=revenue
  units text NOT NULL DEFAULT 'usd',
  description text,
  metadata jsonb DEFAULT '{}'::jsonb,
  pipeline_run_id uuid REFERENCES public.pipeline_runs(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_cost_ledger_date ON public.cost_ledger(ledger_date DESC);
CREATE INDEX idx_cost_ledger_category ON public.cost_ledger(category, ledger_date DESC);

-- ============================================================================
-- Table 5: budget_config — Configurable budget caps
-- ============================================================================

CREATE TABLE public.budget_config (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  config_key text UNIQUE NOT NULL,
  value numeric(12,4) NOT NULL,
  description text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by text DEFAULT 'system'
);

-- Default budget caps
INSERT INTO public.budget_config (config_key, value, description) VALUES
  ('daily_claude_budget', 0.50, 'Max daily spend on Claude API calls (USD)'),
  ('daily_perplexity_budget', 0.10, 'Max daily spend on Perplexity API calls (USD)');

-- ============================================================================
-- Table 6: confidence_calibration — Weekly stated vs actual tracking
-- ============================================================================

CREATE TABLE public.confidence_calibration (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  calibration_week date NOT NULL,
  buckets jsonb NOT NULL DEFAULT '[]'::jsonb,
  total_predictions integer NOT NULL DEFAULT 0,
  total_graded integer NOT NULL DEFAULT 0,
  brier_score numeric(6,4),
  calibration_error numeric(6,4),
  overconfidence_bias numeric(6,4),
  active_factors jsonb DEFAULT '{}'::jsonb,
  depth_factors jsonb DEFAULT '{}'::jsonb,
  pipeline_run_id uuid REFERENCES public.pipeline_runs(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_confidence_cal_week ON public.confidence_calibration(calibration_week DESC);

-- ============================================================================
-- RAG Functions
-- ============================================================================

-- Match catalyst events with blended similarity + recency ranking
CREATE OR REPLACE FUNCTION public.match_catalyst_events(
  query_embedding extensions.vector(768),
  match_threshold float DEFAULT 0.5,
  match_count int DEFAULT 10,
  filter_type text DEFAULT NULL,
  filter_ticker text DEFAULT NULL
)
RETURNS TABLE (
  id uuid,
  ticker text,
  catalyst_type text,
  headline text,
  source text,
  event_time timestamptz,
  magnitude text,
  direction text,
  sentiment_score numeric,
  affected_tickers text[],
  sector text,
  actual_impact_pct numeric,
  content text,
  similarity float,
  blended_score float
)
LANGUAGE sql STABLE
AS $$
  SELECT
    ce.id, ce.ticker, ce.catalyst_type, ce.headline, ce.source,
    ce.event_time, ce.magnitude, ce.direction, ce.sentiment_score,
    ce.affected_tickers, ce.sector, ce.actual_impact_pct, ce.content,
    (1 - (ce.embedding <=> query_embedding))::float AS similarity,
    -- 60% similarity + 40% recency (decays over 30 days)
    (0.6 * (1 - (ce.embedding <=> query_embedding)) +
     0.4 * GREATEST(0, 1 - EXTRACT(EPOCH FROM (now() - ce.event_time)) / (30 * 86400))
    )::float AS blended_score
  FROM public.catalyst_events ce
  WHERE ce.embedding IS NOT NULL
    AND 1 - (ce.embedding <=> query_embedding) > match_threshold
    AND (filter_type IS NULL OR ce.catalyst_type = filter_type)
    AND (filter_ticker IS NULL OR ce.ticker = filter_ticker
         OR filter_ticker = ANY(ce.affected_tickers))
  ORDER BY blended_score DESC
  LIMIT match_count;
$$;

-- Match inference chains for pattern recognition
CREATE OR REPLACE FUNCTION public.match_inference_chains(
  query_embedding extensions.vector(768),
  match_threshold float DEFAULT 0.6,
  match_count int DEFAULT 5
)
RETURNS TABLE (
  id uuid,
  ticker text,
  chain_date date,
  max_depth_reached integer,
  final_confidence numeric,
  final_decision text,
  stopping_reason text,
  tumblers jsonb,
  actual_outcome text,
  actual_pnl numeric,
  reasoning_summary text,
  similarity float
)
LANGUAGE sql STABLE
AS $$
  SELECT
    ic.id, ic.ticker, ic.chain_date, ic.max_depth_reached,
    ic.final_confidence, ic.final_decision, ic.stopping_reason,
    ic.tumblers, ic.actual_outcome, ic.actual_pnl, ic.reasoning_summary,
    (1 - (ic.embedding <=> query_embedding))::float AS similarity
  FROM public.inference_chains ic
  WHERE ic.embedding IS NOT NULL
    AND 1 - (ic.embedding <=> query_embedding) > match_threshold
  ORDER BY ic.embedding <=> query_embedding
  LIMIT match_count;
$$;

-- Match pattern templates
CREATE OR REPLACE FUNCTION public.match_pattern_templates(
  query_embedding extensions.vector(768),
  match_threshold float DEFAULT 0.6,
  match_count int DEFAULT 5
)
RETURNS TABLE (
  id uuid,
  pattern_name text,
  pattern_description text,
  pattern_category text,
  trigger_conditions jsonb,
  times_matched integer,
  success_rate numeric,
  avg_return_pct numeric,
  template_confidence numeric,
  status text,
  similarity float
)
LANGUAGE sql STABLE
AS $$
  SELECT
    pt.id, pt.pattern_name, pt.pattern_description, pt.pattern_category,
    pt.trigger_conditions, pt.times_matched, pt.success_rate,
    pt.avg_return_pct, pt.template_confidence, pt.status,
    (1 - (pt.embedding <=> query_embedding))::float AS similarity
  FROM public.pattern_templates pt
  WHERE pt.embedding IS NOT NULL
    AND pt.status = 'active'
    AND 1 - (pt.embedding <=> query_embedding) > match_threshold
  ORDER BY pt.embedding <=> query_embedding
  LIMIT match_count;
$$;

-- ============================================================================
-- RLS Policies
-- ============================================================================

ALTER TABLE public.catalyst_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pattern_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.inference_chains ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cost_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.budget_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.confidence_calibration ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role manages catalyst_events" ON public.catalyst_events FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages pattern_templates" ON public.pattern_templates FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages inference_chains" ON public.inference_chains FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages cost_ledger" ON public.cost_ledger FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages budget_config" ON public.budget_config FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages confidence_calibration" ON public.confidence_calibration FOR ALL USING (true) WITH CHECK (true);

-- ============================================================================
-- Retention Policies
-- ============================================================================

-- catalyst_events: 1 year retention
SELECT cron.schedule(
  'purge-old-catalyst-events',
  '15 4 * * *',
  $$DELETE FROM public.catalyst_events WHERE created_at < now() - interval '365 days'$$
);

-- inference_chains: 1 year retention
SELECT cron.schedule(
  'purge-old-inference-chains',
  '20 4 * * *',
  $$DELETE FROM public.inference_chains WHERE created_at < now() - interval '365 days'$$
);

-- cost_ledger: 2 year retention (need long history for economics)
SELECT cron.schedule(
  'purge-old-cost-ledger',
  '25 4 * * *',
  $$DELETE FROM public.cost_ledger WHERE created_at < now() - interval '730 days'$$
);

-- confidence_calibration: 1 year retention
SELECT cron.schedule(
  'purge-old-confidence-calibration',
  '30 4 * * *',
  $$DELETE FROM public.confidence_calibration WHERE created_at < now() - interval '365 days'$$
);
