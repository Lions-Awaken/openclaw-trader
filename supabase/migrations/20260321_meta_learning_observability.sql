-- Meta-Learning & Observability Migration
-- Phase 1: pipeline_runs, order_events, data_quality_checks
-- Phase 2: signal_evaluations + view + RAG function
-- Phase 3: meta_reflections, strategy_adjustments + RAG function

-- Enable pgvector if not already enabled
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;

-- ============================================================================
-- Phase 1: Observability Foundation
-- ============================================================================

-- Pipeline execution tree — every automated function call
CREATE TABLE public.pipeline_runs (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  pipeline_name text NOT NULL,
  step_name text NOT NULL,
  parent_run_id uuid REFERENCES public.pipeline_runs(id) ON DELETE SET NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'running', 'success', 'failure', 'skipped', 'timeout')),
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  duration_ms integer GENERATED ALWAYS AS (
    CASE WHEN completed_at IS NOT NULL
      THEN EXTRACT(EPOCH FROM (completed_at - started_at))::integer * 1000
      ELSE NULL
    END
  ) STORED,
  input_snapshot jsonb DEFAULT '{}'::jsonb,
  output_snapshot jsonb DEFAULT '{}'::jsonb,
  error_message text,
  error_traceback text,
  metadata jsonb DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_pipeline_runs_name ON public.pipeline_runs(pipeline_name, started_at DESC);
CREATE INDEX idx_pipeline_runs_status ON public.pipeline_runs(status, started_at DESC);
CREATE INDEX idx_pipeline_runs_parent ON public.pipeline_runs(parent_run_id);
CREATE INDEX idx_pipeline_runs_created ON public.pipeline_runs(created_at DESC);

-- Order lifecycle events
CREATE TABLE public.order_events (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  order_id text NOT NULL,
  ticker text NOT NULL,
  event_type text NOT NULL
    CHECK (event_type IN ('submitted', 'filled', 'partial_fill', 'rejected', 'cancelled', 'expired', 'replaced')),
  side text NOT NULL CHECK (side IN ('buy', 'sell')),
  qty_ordered numeric(12,4),
  qty_filled numeric(12,4),
  price numeric(12,4),
  avg_fill_price numeric(12,4),
  raw_event jsonb DEFAULT '{}'::jsonb,
  pipeline_run_id uuid REFERENCES public.pipeline_runs(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_order_events_order ON public.order_events(order_id, created_at);
CREATE INDEX idx_order_events_ticker ON public.order_events(ticker, created_at DESC);
CREATE INDEX idx_order_events_pipeline ON public.order_events(pipeline_run_id);

-- Data quality / freshness checks
CREATE TABLE public.data_quality_checks (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  check_name text NOT NULL,
  target text NOT NULL,
  passed boolean NOT NULL,
  expected_value text,
  actual_value text,
  severity text NOT NULL DEFAULT 'warning'
    CHECK (severity IN ('info', 'warning', 'critical')),
  pipeline_run_id uuid REFERENCES public.pipeline_runs(id) ON DELETE SET NULL,
  checked_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_dq_checks_name ON public.data_quality_checks(check_name, checked_at DESC);
CREATE INDEX idx_dq_checks_failed ON public.data_quality_checks(passed, checked_at DESC) WHERE NOT passed;

-- ============================================================================
-- Phase 2: Signal Tracing
-- ============================================================================

-- Per-ticker per-scan signal evaluation detail
CREATE TABLE public.signal_evaluations (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  ticker text NOT NULL,
  scan_date date NOT NULL DEFAULT CURRENT_DATE,
  scan_type text NOT NULL DEFAULT 'pre_market'
    CHECK (scan_type IN ('pre_market', 'midday', 'close', 'manual')),

  -- Each signal stored as JSONB with raw indicator values + passed flag
  trend jsonb DEFAULT '{}'::jsonb,
  momentum jsonb DEFAULT '{}'::jsonb,
  volume jsonb DEFAULT '{}'::jsonb,
  fundamental jsonb DEFAULT '{}'::jsonb,
  sentiment jsonb DEFAULT '{}'::jsonb,
  flow jsonb DEFAULT '{}'::jsonb,

  total_score integer NOT NULL DEFAULT 0 CHECK (total_score BETWEEN 0 AND 6),
  decision text NOT NULL DEFAULT 'skip'
    CHECK (decision IN ('enter', 'skip', 'watch', 'veto')),
  reasoning text,

  -- pgvector embedding for RAG retrieval
  embedding extensions.vector(768),

  pipeline_run_id uuid REFERENCES public.pipeline_runs(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_signal_evals_ticker ON public.signal_evaluations(ticker, scan_date DESC);
CREATE INDEX idx_signal_evals_decision ON public.signal_evaluations(decision, scan_date DESC);
CREATE INDEX idx_signal_evals_score ON public.signal_evaluations(total_score, scan_date DESC);
CREATE INDEX idx_signal_evals_date ON public.signal_evaluations(scan_date DESC);

-- HNSW index for vector similarity search
CREATE INDEX idx_signal_evals_embedding ON public.signal_evaluations
  USING hnsw (embedding extensions.vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- Signal accuracy report view — weekly accuracy per signal
CREATE OR REPLACE VIEW public.signal_accuracy_report AS
WITH evals AS (
  SELECT
    date_trunc('week', se.scan_date)::date AS week_start,
    se.ticker,
    se.trend,
    se.momentum,
    se.volume,
    se.fundamental,
    se.sentiment,
    se.flow,
    se.decision,
    se.total_score,
    -- Join to trade_decisions to see if this scan led to a profitable trade
    td.pnl,
    td.outcome
  FROM public.signal_evaluations se
  LEFT JOIN public.trade_decisions td
    ON td.ticker = se.ticker
    AND td.created_at::date BETWEEN se.scan_date AND se.scan_date + interval '5 days'
    AND td.action = 'BUY'
)
SELECT
  week_start,
  COUNT(*) AS total_evaluations,
  COUNT(*) FILTER (WHERE decision = 'enter') AS entries,
  COUNT(*) FILTER (WHERE pnl IS NOT NULL AND pnl > 0) AS profitable_entries,
  -- Per-signal accuracy (signal fired AND trade was profitable)
  ROUND(AVG(CASE WHEN (trend->>'passed')::boolean AND pnl > 0 THEN 1.0
                 WHEN (trend->>'passed')::boolean AND pnl <= 0 THEN 0.0
                 ELSE NULL END) * 100, 1) AS trend_accuracy,
  ROUND(AVG(CASE WHEN (momentum->>'passed')::boolean AND pnl > 0 THEN 1.0
                 WHEN (momentum->>'passed')::boolean AND pnl <= 0 THEN 0.0
                 ELSE NULL END) * 100, 1) AS momentum_accuracy,
  ROUND(AVG(CASE WHEN (volume->>'passed')::boolean AND pnl > 0 THEN 1.0
                 WHEN (volume->>'passed')::boolean AND pnl <= 0 THEN 0.0
                 ELSE NULL END) * 100, 1) AS volume_accuracy,
  ROUND(AVG(CASE WHEN (fundamental->>'passed')::boolean AND pnl > 0 THEN 1.0
                 WHEN (fundamental->>'passed')::boolean AND pnl <= 0 THEN 0.0
                 ELSE NULL END) * 100, 1) AS fundamental_accuracy,
  ROUND(AVG(CASE WHEN (sentiment->>'passed')::boolean AND pnl > 0 THEN 1.0
                 WHEN (sentiment->>'passed')::boolean AND pnl <= 0 THEN 0.0
                 ELSE NULL END) * 100, 1) AS sentiment_accuracy,
  ROUND(AVG(CASE WHEN (flow->>'passed')::boolean AND pnl > 0 THEN 1.0
                 WHEN (flow->>'passed')::boolean AND pnl <= 0 THEN 0.0
                 ELSE NULL END) * 100, 1) AS flow_accuracy
FROM evals
GROUP BY week_start
ORDER BY week_start DESC;

-- RAG similarity search for signal evaluations
CREATE OR REPLACE FUNCTION public.match_signal_evaluations(
  query_embedding extensions.vector(768),
  match_threshold float DEFAULT 0.7,
  match_count int DEFAULT 5
)
RETURNS TABLE (
  id uuid,
  ticker text,
  scan_date date,
  total_score integer,
  decision text,
  reasoning text,
  trend jsonb,
  momentum jsonb,
  volume jsonb,
  fundamental jsonb,
  sentiment jsonb,
  flow jsonb,
  similarity float
)
LANGUAGE sql STABLE
AS $$
  SELECT
    se.id, se.ticker, se.scan_date, se.total_score, se.decision, se.reasoning,
    se.trend, se.momentum, se.volume, se.fundamental, se.sentiment, se.flow,
    1 - (se.embedding <=> query_embedding) AS similarity
  FROM public.signal_evaluations se
  WHERE se.embedding IS NOT NULL
    AND 1 - (se.embedding <=> query_embedding) > match_threshold
  ORDER BY se.embedding <=> query_embedding
  LIMIT match_count;
$$;

-- ============================================================================
-- Phase 3: Meta-Learning Pipeline
-- ============================================================================

-- Daily/weekly meta-analysis reflections
CREATE TABLE public.meta_reflections (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  reflection_date date NOT NULL,
  reflection_type text NOT NULL
    CHECK (reflection_type IN ('daily', 'weekly', 'incident', 'strategy_review')),

  -- Summary data
  pipeline_summary jsonb DEFAULT '{}'::jsonb,
  signal_accuracy jsonb DEFAULT '{}'::jsonb,

  -- LLM-generated analysis
  patterns_observed text,
  signal_assessment text,
  operational_issues text,
  counterfactuals text,

  -- Proposed changes
  adjustments jsonb DEFAULT '[]'::jsonb,

  -- pgvector embedding for RAG
  embedding extensions.vector(768),

  pipeline_run_id uuid REFERENCES public.pipeline_runs(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_meta_reflections_date ON public.meta_reflections(reflection_date DESC);
CREATE INDEX idx_meta_reflections_type ON public.meta_reflections(reflection_type, reflection_date DESC);

CREATE INDEX idx_meta_reflections_embedding ON public.meta_reflections
  USING hnsw (embedding extensions.vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- Strategy parameter adjustments log
CREATE TABLE public.strategy_adjustments (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  parameter_name text NOT NULL,
  previous_value text NOT NULL,
  new_value text NOT NULL,
  reason text NOT NULL,
  status text NOT NULL DEFAULT 'proposed'
    CHECK (status IN ('proposed', 'approved', 'applied', 'rejected', 'reverted')),
  impact_assessment text,
  trades_since_applied integer DEFAULT 0,
  pnl_since_applied numeric(12,4),
  meta_reflection_id uuid REFERENCES public.meta_reflections(id) ON DELETE SET NULL,
  applied_at timestamptz,
  reverted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_strategy_adj_status ON public.strategy_adjustments(status, created_at DESC);
CREATE INDEX idx_strategy_adj_param ON public.strategy_adjustments(parameter_name, created_at DESC);

-- RAG similarity search for meta reflections
CREATE OR REPLACE FUNCTION public.match_meta_reflections(
  query_embedding extensions.vector(768),
  match_threshold float DEFAULT 0.7,
  match_count int DEFAULT 5
)
RETURNS TABLE (
  id uuid,
  reflection_date date,
  reflection_type text,
  patterns_observed text,
  signal_assessment text,
  operational_issues text,
  counterfactuals text,
  adjustments jsonb,
  similarity float
)
LANGUAGE sql STABLE
AS $$
  SELECT
    mr.id, mr.reflection_date, mr.reflection_type,
    mr.patterns_observed, mr.signal_assessment, mr.operational_issues,
    mr.counterfactuals, mr.adjustments,
    1 - (mr.embedding <=> query_embedding) AS similarity
  FROM public.meta_reflections mr
  WHERE mr.embedding IS NOT NULL
    AND 1 - (mr.embedding <=> query_embedding) > match_threshold
  ORDER BY mr.embedding <=> query_embedding
  LIMIT match_count;
$$;

-- ============================================================================
-- RLS Policies (same pattern as existing tables)
-- ============================================================================

ALTER TABLE public.pipeline_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.order_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.data_quality_checks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.signal_evaluations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.meta_reflections ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.strategy_adjustments ENABLE ROW LEVEL SECURITY;

-- Service role full access on all tables
CREATE POLICY "Service role manages pipeline_runs" ON public.pipeline_runs FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages order_events" ON public.order_events FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages data_quality_checks" ON public.data_quality_checks FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages signal_evaluations" ON public.signal_evaluations FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages meta_reflections" ON public.meta_reflections FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages strategy_adjustments" ON public.strategy_adjustments FOR ALL USING (true) WITH CHECK (true);

-- ============================================================================
-- Retention: purge pipeline_runs older than 90 days, signal_evaluations > 1 year
-- ============================================================================

SELECT cron.schedule(
  'purge-old-pipeline-runs',
  '0 4 * * *',
  $$DELETE FROM public.pipeline_runs WHERE created_at < now() - interval '90 days'$$
);

SELECT cron.schedule(
  'purge-old-data-quality-checks',
  '5 4 * * *',
  $$DELETE FROM public.data_quality_checks WHERE checked_at < now() - interval '90 days'$$
);

SELECT cron.schedule(
  'purge-old-order-events',
  '10 4 * * *',
  $$DELETE FROM public.order_events WHERE created_at < now() - interval '180 days'$$
);
