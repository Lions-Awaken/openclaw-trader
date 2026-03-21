-- Hardware Performance Tuning System
-- Tracks versioned tuning profiles, per-run telemetry, and correlates
-- hardware configuration with pipeline outcomes and trading performance.
--
-- Tables: tuning_profiles, tuning_telemetry
-- Adds: tuning_profile_id FK on pipeline_runs

-- ============================================================================
-- Table 1: tuning_profiles — Versioned hardware configuration snapshots
-- ============================================================================

CREATE TABLE public.tuning_profiles (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  version serial UNIQUE,
  profile_name text NOT NULL,
  description text,

  -- Jetson hardware config
  power_mode text,                 -- '15W', '25W', 'MAXN', etc.
  jetson_clocks boolean DEFAULT false,  -- Whether jetson_clocks was enabled
  gpu_freq_mhz integer,           -- GPU clock frequency
  cpu_freq_mhz integer,           -- Max CPU clock frequency
  cpu_cores_online integer,       -- Number of CPU cores active
  fan_mode text,                   -- 'quiet', 'cool', 'auto'
  fan_speed_pct integer,          -- Fixed fan speed if not auto

  -- Ollama / LLM config
  ollama_config jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- Expected keys: {
  --   "qwen_ctx_size": 2048,
  --   "qwen_num_gpu": 99,
  --   "qwen_num_thread": 4,
  --   "qwen_batch_size": 512,
  --   "qwen_keep_alive": "0",
  --   "embed_model": "nomic-embed-text",
  --   "embed_keep_alive": "0"
  -- }

  -- Embedding pipeline config
  embedding_config jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- Expected keys: {
  --   "model": "nomic-embed-text",
  --   "batch_size": 1,
  --   "max_concurrent": 1
  -- }

  -- System-level config
  system_config jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- Expected keys: {
  --   "swap_size_gb": 8,
  --   "zram_enabled": true,
  --   "oom_score_adj": {},
  --   "numa_policy": "",
  --   "io_scheduler": "mq-deadline"
  -- }

  -- Lifecycle
  status text NOT NULL DEFAULT 'testing'
    CHECK (status IN ('active', 'testing', 'retired', 'baseline')),
  activated_at timestamptz,
  retired_at timestamptz,
  notes text,

  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_tuning_profiles_status ON public.tuning_profiles(status);
CREATE INDEX idx_tuning_profiles_version ON public.tuning_profiles(version DESC);

-- Insert the baseline profile (what the system runs before any tuning)
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
);

-- ============================================================================
-- Table 2: tuning_telemetry — Per-pipeline-run hardware snapshots
-- ============================================================================

CREATE TABLE public.tuning_telemetry (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  pipeline_run_id uuid NOT NULL REFERENCES public.pipeline_runs(id) ON DELETE CASCADE,
  tuning_profile_id uuid NOT NULL REFERENCES public.tuning_profiles(id) ON DELETE SET NULL,

  -- Memory telemetry
  ram_start_mb numeric(8,1),       -- RSS at pipeline start
  ram_peak_mb numeric(8,1),        -- Peak RSS during execution
  ram_end_mb numeric(8,1),         -- RSS at pipeline end
  gpu_mem_start_mb numeric(8,1),   -- GPU mem at start (shared on Jetson)
  gpu_mem_peak_mb numeric(8,1),    -- Peak GPU mem

  -- Compute telemetry
  avg_cpu_pct numeric(5,1),        -- Average CPU utilization during run
  max_cpu_pct numeric(5,1),        -- Peak CPU
  avg_gpu_pct numeric(5,1),        -- Average GPU utilization
  max_gpu_pct numeric(5,1),        -- Peak GPU

  -- Thermal telemetry
  cpu_temp_start_c numeric(4,1),
  cpu_temp_max_c numeric(4,1),
  gpu_temp_start_c numeric(4,1),
  gpu_temp_max_c numeric(4,1),
  thermal_throttle_events integer DEFAULT 0,  -- Number of throttle events during run

  -- Power telemetry
  power_draw_avg_watts numeric(5,2),
  power_draw_max_watts numeric(5,2),

  -- LLM-specific telemetry
  ollama_inference_count integer DEFAULT 0,   -- Number of Ollama calls
  ollama_tokens_generated integer DEFAULT 0,  -- Total tokens produced
  ollama_avg_tokens_per_sec numeric(6,1),     -- Average generation speed
  ollama_min_tokens_per_sec numeric(6,1),     -- Slowest call
  ollama_max_tokens_per_sec numeric(6,1),     -- Fastest call
  embedding_count integer DEFAULT 0,          -- Number of embeddings generated
  embedding_avg_ms integer,                   -- Average embedding time
  embedding_max_ms integer,                   -- Slowest embedding

  -- Claude API telemetry (for remote calls)
  claude_call_count integer DEFAULT 0,
  claude_total_tokens integer DEFAULT 0,
  claude_avg_latency_ms integer,              -- Network round-trip

  -- Overall timing
  wall_clock_ms integer,                      -- Total pipeline wall-clock time
  pipeline_name text NOT NULL,
  step_count integer DEFAULT 0,               -- Number of steps in this run

  -- Flexible extras
  metadata jsonb DEFAULT '{}'::jsonb,
  -- Could include: { "oom_killed": false, "disk_io_mb": 12.5, "context_switches": 500 }

  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_tuning_telemetry_pipeline ON public.tuning_telemetry(pipeline_run_id);
CREATE INDEX idx_tuning_telemetry_profile ON public.tuning_telemetry(tuning_profile_id, created_at DESC);
CREATE INDEX idx_tuning_telemetry_created ON public.tuning_telemetry(created_at DESC);
CREATE INDEX idx_tuning_telemetry_pipeline_name ON public.tuning_telemetry(pipeline_name, created_at DESC);

-- ============================================================================
-- Add tuning_profile_id to pipeline_runs
-- ============================================================================

ALTER TABLE public.pipeline_runs
  ADD COLUMN tuning_profile_id uuid REFERENCES public.tuning_profiles(id) ON DELETE SET NULL;

CREATE INDEX idx_pipeline_runs_tuning ON public.pipeline_runs(tuning_profile_id);

-- ============================================================================
-- Tuning comparison view — aggregates per-profile performance
-- ============================================================================

CREATE OR REPLACE VIEW public.tuning_profile_performance AS
SELECT
  tp.id AS profile_id,
  tp.version,
  tp.profile_name,
  tp.power_mode,
  tp.status,
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
  -- Join to inference chains for outcome correlation
  COUNT(ic.id) AS total_chains,
  COUNT(ic.id) FILTER (WHERE ic.actual_outcome IN ('STRONG_WIN', 'WIN')) AS chain_wins,
  ROUND(
    CASE WHEN COUNT(ic.id) > 0
      THEN COUNT(ic.id) FILTER (WHERE ic.actual_outcome IN ('STRONG_WIN', 'WIN'))::numeric / COUNT(ic.id) * 100
      ELSE NULL
    END, 1
  ) AS chain_win_rate_pct,
  ROUND(AVG(ic.final_confidence), 3) AS avg_chain_confidence
FROM public.tuning_profiles tp
LEFT JOIN public.tuning_telemetry tt ON tt.tuning_profile_id = tp.id
LEFT JOIN public.pipeline_runs pr ON pr.id = tt.pipeline_run_id AND pr.step_name = 'root'
LEFT JOIN public.inference_chains ic ON ic.pipeline_run_id = pr.id
GROUP BY tp.id, tp.version, tp.profile_name, tp.power_mode, tp.status
ORDER BY tp.version DESC;

-- ============================================================================
-- RLS Policies
-- ============================================================================

ALTER TABLE public.tuning_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tuning_telemetry ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role manages tuning_profiles" ON public.tuning_profiles FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role manages tuning_telemetry" ON public.tuning_telemetry FOR ALL USING (true) WITH CHECK (true);

-- ============================================================================
-- Retention: telemetry kept 1 year
-- ============================================================================

SELECT cron.schedule(
  'purge-old-tuning-telemetry',
  '35 4 * * *',
  $$DELETE FROM public.tuning_telemetry WHERE created_at < now() - interval '365 days'$$
);
