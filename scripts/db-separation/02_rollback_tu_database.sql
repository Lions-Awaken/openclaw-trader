-- ============================================================================
-- ROLLBACK: Recreate openclaw-trader tables in Twilight Underground database
-- Target: uupmzaglafeiakamefit (Twilight Underground)
--
-- Use this script if anything breaks after running 01_cleanup_tu_database.sql.
-- This recreates the exact schema that was removed.
-- ============================================================================

BEGIN;

-- ============================================================================
-- Extensions (should already exist, but ensure)
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS pg_cron WITH SCHEMA pg_catalog;
GRANT USAGE ON SCHEMA cron TO postgres;

-- ============================================================================
-- Phase 1: Observability Foundation
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.pipeline_runs (
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

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_name ON public.pipeline_runs(pipeline_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status ON public.pipeline_runs(status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_parent ON public.pipeline_runs(parent_run_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_created ON public.pipeline_runs(created_at DESC);

CREATE TABLE IF NOT EXISTS public.order_events (
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

CREATE INDEX IF NOT EXISTS idx_order_events_order ON public.order_events(order_id, created_at);
CREATE INDEX IF NOT EXISTS idx_order_events_ticker ON public.order_events(ticker, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_order_events_pipeline ON public.order_events(pipeline_run_id);

CREATE TABLE IF NOT EXISTS public.data_quality_checks (
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

CREATE INDEX IF NOT EXISTS idx_dq_checks_name ON public.data_quality_checks(check_name, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_dq_checks_failed ON public.data_quality_checks(passed, checked_at DESC) WHERE NOT passed;

-- ============================================================================
-- Phase 2: Signal Tracing
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.signal_evaluations (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  ticker text NOT NULL,
  scan_date date NOT NULL DEFAULT CURRENT_DATE,
  scan_type text NOT NULL DEFAULT 'pre_market'
    CHECK (scan_type IN ('pre_market', 'midday', 'close', 'manual')),
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
  embedding extensions.vector(768),
  pipeline_run_id uuid REFERENCES public.pipeline_runs(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_signal_evals_ticker ON public.signal_evaluations(ticker, scan_date DESC);
CREATE INDEX IF NOT EXISTS idx_signal_evals_decision ON public.signal_evaluations(decision, scan_date DESC);
CREATE INDEX IF NOT EXISTS idx_signal_evals_score ON public.signal_evaluations(total_score, scan_date DESC);
CREATE INDEX IF NOT EXISTS idx_signal_evals_date ON public.signal_evaluations(scan_date DESC);
CREATE INDEX IF NOT EXISTS idx_signal_evals_embedding ON public.signal_evaluations
  USING hnsw (embedding extensions.vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- ============================================================================
-- Phase 3: Meta-Learning
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.meta_reflections (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  reflection_date date NOT NULL,
  reflection_type text NOT NULL
    CHECK (reflection_type IN ('daily', 'weekly', 'incident', 'strategy_review')),
  pipeline_summary jsonb DEFAULT '{}'::jsonb,
  signal_accuracy jsonb DEFAULT '{}'::jsonb,
  patterns_observed text,
  signal_assessment text,
  operational_issues text,
  counterfactuals text,
  adjustments jsonb DEFAULT '[]'::jsonb,
  embedding extensions.vector(768),
  pipeline_run_id uuid REFERENCES public.pipeline_runs(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_meta_reflections_date ON public.meta_reflections(reflection_date DESC);
CREATE INDEX IF NOT EXISTS idx_meta_reflections_type ON public.meta_reflections(reflection_type, reflection_date DESC);
CREATE INDEX IF NOT EXISTS idx_meta_reflections_embedding ON public.meta_reflections
  USING hnsw (embedding extensions.vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

CREATE TABLE IF NOT EXISTS public.strategy_adjustments (
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

CREATE INDEX IF NOT EXISTS idx_strategy_adj_status ON public.strategy_adjustments(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_adj_param ON public.strategy_adjustments(parameter_name, created_at DESC);

-- ============================================================================
-- Phase 4: Tumbler Architecture
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.pattern_templates (
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

CREATE INDEX IF NOT EXISTS idx_pattern_templates_category ON public.pattern_templates(pattern_category, status);
CREATE INDEX IF NOT EXISTS idx_pattern_templates_status ON public.pattern_templates(status, success_rate DESC);
CREATE INDEX IF NOT EXISTS idx_pattern_templates_embedding ON public.pattern_templates
  USING hnsw (embedding extensions.vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

CREATE TABLE IF NOT EXISTS public.catalyst_events (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  ticker text,
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
  pattern_template_id uuid REFERENCES public.pattern_templates(id) ON DELETE SET NULL,
  content text,
  embedding extensions.vector(768),
  metadata jsonb DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_catalyst_events_ticker ON public.catalyst_events(ticker, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_catalyst_events_type ON public.catalyst_events(catalyst_type, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_catalyst_events_time ON public.catalyst_events(event_time DESC);
CREATE INDEX IF NOT EXISTS idx_catalyst_events_direction ON public.catalyst_events(direction, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_catalyst_events_magnitude ON public.catalyst_events(magnitude, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_catalyst_events_embedding ON public.catalyst_events
  USING hnsw (embedding extensions.vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

CREATE TABLE IF NOT EXISTS public.inference_chains (
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

CREATE INDEX IF NOT EXISTS idx_inference_chains_ticker ON public.inference_chains(ticker, chain_date DESC);
CREATE INDEX IF NOT EXISTS idx_inference_chains_date ON public.inference_chains(chain_date DESC);
CREATE INDEX IF NOT EXISTS idx_inference_chains_decision ON public.inference_chains(final_decision, chain_date DESC);
CREATE INDEX IF NOT EXISTS idx_inference_chains_depth ON public.inference_chains(max_depth_reached, chain_date DESC);
CREATE INDEX IF NOT EXISTS idx_inference_chains_signal ON public.inference_chains(signal_evaluation_id);
CREATE INDEX IF NOT EXISTS idx_inference_chains_embedding ON public.inference_chains
  USING hnsw (embedding extensions.vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

CREATE TABLE IF NOT EXISTS public.cost_ledger (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  ledger_date date NOT NULL DEFAULT CURRENT_DATE,
  category text NOT NULL
    CHECK (category IN (
      'claude_api', 'perplexity_api', 'finnhub_api',
      'fly_hosting', 'supabase', 'ollama_power', 'trade_pnl'
    )),
  subcategory text,
  amount numeric(12,4) NOT NULL,
  units text NOT NULL DEFAULT 'usd',
  description text,
  metadata jsonb DEFAULT '{}'::jsonb,
  pipeline_run_id uuid REFERENCES public.pipeline_runs(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cost_ledger_date ON public.cost_ledger(ledger_date DESC);
CREATE INDEX IF NOT EXISTS idx_cost_ledger_category ON public.cost_ledger(category, ledger_date DESC);

CREATE TABLE IF NOT EXISTS public.budget_config (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  config_key text UNIQUE NOT NULL,
  value numeric(12,4) NOT NULL,
  description text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by text DEFAULT 'system'
);

INSERT INTO public.budget_config (config_key, value, description) VALUES
  ('daily_claude_budget', 0.50, 'Max daily spend on Claude API calls (USD)'),
  ('daily_perplexity_budget', 0.10, 'Max daily spend on Perplexity API calls (USD)')
ON CONFLICT (config_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS public.confidence_calibration (
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

CREATE INDEX IF NOT EXISTS idx_confidence_cal_week ON public.confidence_calibration(calibration_week DESC);

-- ============================================================================
-- Phase 5: Tuning System
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.tuning_profiles (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  version serial UNIQUE,
  profile_name text NOT NULL,
  description text,
  power_mode text,
  jetson_clocks boolean DEFAULT false,
  gpu_freq_mhz integer,
  cpu_freq_mhz integer,
  cpu_cores_online integer,
  fan_mode text,
  fan_speed_pct integer,
  ollama_config jsonb NOT NULL DEFAULT '{}'::jsonb,
  embedding_config jsonb NOT NULL DEFAULT '{}'::jsonb,
  system_config jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'testing'
    CHECK (status IN ('active', 'testing', 'retired', 'baseline')),
  activated_at timestamptz,
  retired_at timestamptz,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tuning_profiles_status ON public.tuning_profiles(status);
CREATE INDEX IF NOT EXISTS idx_tuning_profiles_version ON public.tuning_profiles(version DESC);

INSERT INTO public.tuning_profiles (
  profile_name, description, power_mode, jetson_clocks,
  ollama_config, embedding_config, system_config,
  status, activated_at, notes
) VALUES (
  'baseline',
  'Default Jetson Orin Nano configuration. No tuning applied.',
  '15W', false,
  '{"qwen_ctx_size": 2048, "qwen_num_gpu": 99, "qwen_num_thread": 4, "qwen_batch_size": 512, "qwen_keep_alive": "0", "embed_model": "nomic-embed-text", "embed_keep_alive": "0"}',
  '{"model": "nomic-embed-text", "batch_size": 1, "max_concurrent": 1}',
  '{"swap_size_gb": 8, "zram_enabled": true}',
  'baseline', now(),
  'Initial untuned configuration. All performance data should reference this as the comparison baseline.'
) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS public.tuning_telemetry (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  pipeline_run_id uuid NOT NULL REFERENCES public.pipeline_runs(id) ON DELETE CASCADE,
  tuning_profile_id uuid NOT NULL REFERENCES public.tuning_profiles(id) ON DELETE SET NULL,
  ram_start_mb numeric(8,1),
  ram_peak_mb numeric(8,1),
  ram_end_mb numeric(8,1),
  gpu_mem_start_mb numeric(8,1),
  gpu_mem_peak_mb numeric(8,1),
  avg_cpu_pct numeric(5,1),
  max_cpu_pct numeric(5,1),
  avg_gpu_pct numeric(5,1),
  max_gpu_pct numeric(5,1),
  cpu_temp_start_c numeric(4,1),
  cpu_temp_max_c numeric(4,1),
  gpu_temp_start_c numeric(4,1),
  gpu_temp_max_c numeric(4,1),
  thermal_throttle_events integer DEFAULT 0,
  power_draw_avg_watts numeric(5,2),
  power_draw_max_watts numeric(5,2),
  ollama_inference_count integer DEFAULT 0,
  ollama_tokens_generated integer DEFAULT 0,
  ollama_avg_tokens_per_sec numeric(6,1),
  ollama_min_tokens_per_sec numeric(6,1),
  ollama_max_tokens_per_sec numeric(6,1),
  embedding_count integer DEFAULT 0,
  embedding_avg_ms integer,
  embedding_max_ms integer,
  claude_call_count integer DEFAULT 0,
  claude_total_tokens integer DEFAULT 0,
  claude_avg_latency_ms integer,
  wall_clock_ms integer,
  pipeline_name text NOT NULL,
  step_count integer DEFAULT 0,
  metadata jsonb DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tuning_telemetry_pipeline ON public.tuning_telemetry(pipeline_run_id);
CREATE INDEX IF NOT EXISTS idx_tuning_telemetry_profile ON public.tuning_telemetry(tuning_profile_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tuning_telemetry_created ON public.tuning_telemetry(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tuning_telemetry_pipeline_name ON public.tuning_telemetry(pipeline_name, created_at DESC);

-- Add tuning_profile_id FK back to pipeline_runs
ALTER TABLE public.pipeline_runs
  ADD COLUMN IF NOT EXISTS tuning_profile_id uuid REFERENCES public.tuning_profiles(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_tuning ON public.pipeline_runs(tuning_profile_id);

-- ============================================================================
-- Phase 6: RAG Functions
-- ============================================================================

CREATE OR REPLACE FUNCTION public.match_signal_evaluations(
  query_embedding extensions.vector(768),
  match_threshold float DEFAULT 0.7,
  match_count int DEFAULT 5
)
RETURNS TABLE (
  id uuid, ticker text, scan_date date, total_score integer, decision text,
  reasoning text, trend jsonb, momentum jsonb, volume jsonb,
  fundamental jsonb, sentiment jsonb, flow jsonb, similarity float
)
LANGUAGE sql STABLE AS $$
  SELECT se.id, se.ticker, se.scan_date, se.total_score, se.decision, se.reasoning,
    se.trend, se.momentum, se.volume, se.fundamental, se.sentiment, se.flow,
    1 - (se.embedding <=> query_embedding) AS similarity
  FROM public.signal_evaluations se
  WHERE se.embedding IS NOT NULL AND 1 - (se.embedding <=> query_embedding) > match_threshold
  ORDER BY se.embedding <=> query_embedding LIMIT match_count;
$$;

CREATE OR REPLACE FUNCTION public.match_meta_reflections(
  query_embedding extensions.vector(768),
  match_threshold float DEFAULT 0.7,
  match_count int DEFAULT 5
)
RETURNS TABLE (
  id uuid, reflection_date date, reflection_type text, patterns_observed text,
  signal_assessment text, operational_issues text, counterfactuals text,
  adjustments jsonb, similarity float
)
LANGUAGE sql STABLE AS $$
  SELECT mr.id, mr.reflection_date, mr.reflection_type, mr.patterns_observed,
    mr.signal_assessment, mr.operational_issues, mr.counterfactuals, mr.adjustments,
    1 - (mr.embedding <=> query_embedding) AS similarity
  FROM public.meta_reflections mr
  WHERE mr.embedding IS NOT NULL AND 1 - (mr.embedding <=> query_embedding) > match_threshold
  ORDER BY mr.embedding <=> query_embedding LIMIT match_count;
$$;

CREATE OR REPLACE FUNCTION public.match_catalyst_events(
  query_embedding extensions.vector(768),
  match_threshold float DEFAULT 0.5,
  match_count int DEFAULT 10,
  filter_type text DEFAULT NULL,
  filter_ticker text DEFAULT NULL
)
RETURNS TABLE (
  id uuid, ticker text, catalyst_type text, headline text, source text,
  event_time timestamptz, magnitude text, direction text, sentiment_score numeric,
  affected_tickers text[], sector text, actual_impact_pct numeric, content text,
  similarity float, blended_score float
)
LANGUAGE sql STABLE AS $$
  SELECT ce.id, ce.ticker, ce.catalyst_type, ce.headline, ce.source,
    ce.event_time, ce.magnitude, ce.direction, ce.sentiment_score,
    ce.affected_tickers, ce.sector, ce.actual_impact_pct, ce.content,
    (1 - (ce.embedding <=> query_embedding))::float AS similarity,
    (0.6 * (1 - (ce.embedding <=> query_embedding)) +
     0.4 * GREATEST(0, 1 - EXTRACT(EPOCH FROM (now() - ce.event_time)) / (30 * 86400)))::float AS blended_score
  FROM public.catalyst_events ce
  WHERE ce.embedding IS NOT NULL AND 1 - (ce.embedding <=> query_embedding) > match_threshold
    AND (filter_type IS NULL OR ce.catalyst_type = filter_type)
    AND (filter_ticker IS NULL OR ce.ticker = filter_ticker OR filter_ticker = ANY(ce.affected_tickers))
  ORDER BY blended_score DESC LIMIT match_count;
$$;

CREATE OR REPLACE FUNCTION public.match_inference_chains(
  query_embedding extensions.vector(768),
  match_threshold float DEFAULT 0.6,
  match_count int DEFAULT 5
)
RETURNS TABLE (
  id uuid, ticker text, chain_date date, max_depth_reached integer,
  final_confidence numeric, final_decision text, stopping_reason text,
  tumblers jsonb, actual_outcome text, actual_pnl numeric,
  reasoning_summary text, similarity float
)
LANGUAGE sql STABLE AS $$
  SELECT ic.id, ic.ticker, ic.chain_date, ic.max_depth_reached,
    ic.final_confidence, ic.final_decision, ic.stopping_reason,
    ic.tumblers, ic.actual_outcome, ic.actual_pnl, ic.reasoning_summary,
    (1 - (ic.embedding <=> query_embedding))::float AS similarity
  FROM public.inference_chains ic
  WHERE ic.embedding IS NOT NULL AND 1 - (ic.embedding <=> query_embedding) > match_threshold
  ORDER BY ic.embedding <=> query_embedding LIMIT match_count;
$$;

CREATE OR REPLACE FUNCTION public.match_pattern_templates(
  query_embedding extensions.vector(768),
  match_threshold float DEFAULT 0.6,
  match_count int DEFAULT 5
)
RETURNS TABLE (
  id uuid, pattern_name text, pattern_description text, pattern_category text,
  trigger_conditions jsonb, times_matched integer, success_rate numeric,
  avg_return_pct numeric, template_confidence numeric, status text, similarity float
)
LANGUAGE sql STABLE AS $$
  SELECT pt.id, pt.pattern_name, pt.pattern_description, pt.pattern_category,
    pt.trigger_conditions, pt.times_matched, pt.success_rate,
    pt.avg_return_pct, pt.template_confidence, pt.status,
    (1 - (pt.embedding <=> query_embedding))::float AS similarity
  FROM public.pattern_templates pt
  WHERE pt.embedding IS NOT NULL AND pt.status = 'active'
    AND 1 - (pt.embedding <=> query_embedding) > match_threshold
  ORDER BY pt.embedding <=> query_embedding LIMIT match_count;
$$;

-- ============================================================================
-- Phase 7: Views
-- ============================================================================

CREATE OR REPLACE VIEW public.signal_accuracy_report AS
WITH evals AS (
  SELECT
    date_trunc('week', se.scan_date)::date AS week_start,
    se.ticker, se.trend, se.momentum, se.volume, se.fundamental,
    se.sentiment, se.flow, se.decision, se.total_score,
    td.pnl, td.outcome
  FROM public.signal_evaluations se
  LEFT JOIN public.trade_decisions td
    ON td.ticker = se.ticker
    AND td.created_at::date BETWEEN se.scan_date AND se.scan_date + interval '5 days'
    AND td.action = 'BUY'
)
SELECT
  week_start, COUNT(*) AS total_evaluations,
  COUNT(*) FILTER (WHERE decision = 'enter') AS entries,
  COUNT(*) FILTER (WHERE pnl IS NOT NULL AND pnl > 0) AS profitable_entries,
  ROUND(AVG(CASE WHEN (trend->>'passed')::boolean AND pnl > 0 THEN 1.0 WHEN (trend->>'passed')::boolean AND pnl <= 0 THEN 0.0 ELSE NULL END) * 100, 1) AS trend_accuracy,
  ROUND(AVG(CASE WHEN (momentum->>'passed')::boolean AND pnl > 0 THEN 1.0 WHEN (momentum->>'passed')::boolean AND pnl <= 0 THEN 0.0 ELSE NULL END) * 100, 1) AS momentum_accuracy,
  ROUND(AVG(CASE WHEN (volume->>'passed')::boolean AND pnl > 0 THEN 1.0 WHEN (volume->>'passed')::boolean AND pnl <= 0 THEN 0.0 ELSE NULL END) * 100, 1) AS volume_accuracy,
  ROUND(AVG(CASE WHEN (fundamental->>'passed')::boolean AND pnl > 0 THEN 1.0 WHEN (fundamental->>'passed')::boolean AND pnl <= 0 THEN 0.0 ELSE NULL END) * 100, 1) AS fundamental_accuracy,
  ROUND(AVG(CASE WHEN (sentiment->>'passed')::boolean AND pnl > 0 THEN 1.0 WHEN (sentiment->>'passed')::boolean AND pnl <= 0 THEN 0.0 ELSE NULL END) * 100, 1) AS sentiment_accuracy,
  ROUND(AVG(CASE WHEN (flow->>'passed')::boolean AND pnl > 0 THEN 1.0 WHEN (flow->>'passed')::boolean AND pnl <= 0 THEN 0.0 ELSE NULL END) * 100, 1) AS flow_accuracy
FROM evals GROUP BY week_start ORDER BY week_start DESC;

CREATE OR REPLACE VIEW public.tuning_profile_performance AS
SELECT
  tp.id AS profile_id, tp.version, tp.profile_name, tp.power_mode, tp.status,
  COUNT(tt.id) AS total_runs,
  ROUND(AVG(tt.wall_clock_ms), 0) AS avg_wall_clock_ms,
  ROUND(AVG(tt.ram_peak_mb), 1) AS avg_ram_peak_mb,
  MAX(tt.ram_peak_mb) AS max_ram_peak_mb,
  ROUND(AVG(tt.avg_gpu_pct), 1) AS avg_gpu_pct,
  ROUND(AVG(tt.gpu_temp_max_c), 1) AS avg_gpu_temp_max,
  ROUND(AVG(tt.ollama_avg_tokens_per_sec), 1) AS avg_tokens_per_sec,
  ROUND(AVG(tt.embedding_avg_ms), 0) AS avg_embedding_ms,
  SUM(tt.thermal_throttle_events) AS total_throttle_events,
  ROUND(AVG(tt.power_draw_avg_watts), 2) AS avg_power_watts,
  COUNT(ic.id) AS total_chains,
  COUNT(ic.id) FILTER (WHERE ic.actual_outcome IN ('STRONG_WIN', 'WIN')) AS chain_wins,
  ROUND(CASE WHEN COUNT(ic.id) > 0
    THEN COUNT(ic.id) FILTER (WHERE ic.actual_outcome IN ('STRONG_WIN', 'WIN'))::numeric / COUNT(ic.id) * 100
    ELSE NULL END, 1) AS chain_win_rate_pct,
  ROUND(AVG(ic.final_confidence), 3) AS avg_chain_confidence
FROM public.tuning_profiles tp
LEFT JOIN public.tuning_telemetry tt ON tt.tuning_profile_id = tp.id
LEFT JOIN public.pipeline_runs pr ON pr.id = tt.pipeline_run_id AND pr.step_name = 'root'
LEFT JOIN public.inference_chains ic ON ic.pipeline_run_id = pr.id
GROUP BY tp.id, tp.version, tp.profile_name, tp.power_mode, tp.status
ORDER BY tp.version DESC;

-- ============================================================================
-- Phase 8: RLS Policies
-- ============================================================================

ALTER TABLE public.pipeline_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.order_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.data_quality_checks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.signal_evaluations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.meta_reflections ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.strategy_adjustments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.catalyst_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pattern_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.inference_chains ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cost_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.budget_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.confidence_calibration ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tuning_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tuning_telemetry ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role manages pipeline_runs" ON public.pipeline_runs FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages order_events" ON public.order_events FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages data_quality_checks" ON public.data_quality_checks FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages signal_evaluations" ON public.signal_evaluations FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages meta_reflections" ON public.meta_reflections FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages strategy_adjustments" ON public.strategy_adjustments FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages catalyst_events" ON public.catalyst_events FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages pattern_templates" ON public.pattern_templates FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages inference_chains" ON public.inference_chains FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages cost_ledger" ON public.cost_ledger FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages budget_config" ON public.budget_config FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages confidence_calibration" ON public.confidence_calibration FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages tuning_profiles" ON public.tuning_profiles FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages tuning_telemetry" ON public.tuning_telemetry FOR ALL USING (true) WITH CHECK (true);

-- ============================================================================
-- Phase 9: pg_cron Retention Jobs
-- ============================================================================

SELECT cron.schedule('purge-old-pipeline-runs', '0 4 * * *',
  $$DELETE FROM public.pipeline_runs WHERE created_at < now() - interval '90 days'$$);
SELECT cron.schedule('purge-old-data-quality-checks', '5 4 * * *',
  $$DELETE FROM public.data_quality_checks WHERE checked_at < now() - interval '90 days'$$);
SELECT cron.schedule('purge-old-order-events', '10 4 * * *',
  $$DELETE FROM public.order_events WHERE created_at < now() - interval '180 days'$$);
SELECT cron.schedule('purge-old-catalyst-events', '15 4 * * *',
  $$DELETE FROM public.catalyst_events WHERE created_at < now() - interval '365 days'$$);
SELECT cron.schedule('purge-old-inference-chains', '20 4 * * *',
  $$DELETE FROM public.inference_chains WHERE created_at < now() - interval '365 days'$$);
SELECT cron.schedule('purge-old-cost-ledger', '25 4 * * *',
  $$DELETE FROM public.cost_ledger WHERE created_at < now() - interval '730 days'$$);
SELECT cron.schedule('purge-old-confidence-calibration', '30 4 * * *',
  $$DELETE FROM public.confidence_calibration WHERE created_at < now() - interval '365 days'$$);
SELECT cron.schedule('purge-old-tuning-telemetry', '35 4 * * *',
  $$DELETE FROM public.tuning_telemetry WHERE created_at < now() - interval '365 days'$$);
SELECT cron.schedule('purge-expired-magic-links', '45 4 * * *',
  $$DELETE FROM public.magic_link_tokens WHERE expires_at < now() - interval '7 days'$$);

COMMIT;

-- Verify
DO $$
DECLARE _count int;
BEGIN
  SELECT count(*) INTO _count FROM information_schema.tables
  WHERE table_schema = 'public' AND table_name IN (
    'pipeline_runs', 'order_events', 'data_quality_checks', 'signal_evaluations',
    'meta_reflections', 'strategy_adjustments', 'catalyst_events', 'pattern_templates',
    'inference_chains', 'cost_ledger', 'budget_config', 'confidence_calibration',
    'tuning_profiles', 'tuning_telemetry'
  );
  RAISE NOTICE 'ROLLBACK COMPLETE: % openclaw tables recreated in TU database.', _count;
END $$;
