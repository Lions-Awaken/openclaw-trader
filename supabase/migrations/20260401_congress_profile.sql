-- ============================================================================
-- CONGRESS_MIRROR Profile: Politician Intelligence + Legislative Calendar
-- ============================================================================

-- Table 1: politician_intel — scored intelligence record for every tracked member
CREATE TABLE public.politician_intel (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  full_name text NOT NULL,
  bioguide_id text UNIQUE,
  chamber text NOT NULL CHECK (chamber IN ('house', 'senate')),
  party text CHECK (party IN ('D', 'R', 'I')),
  state text,
  district text,

  -- Leadership and committee context
  leadership_role text,
  committees text[] DEFAULT '{}',
  sector_expertise text[] DEFAULT '{}',  -- e.g. ['semiconductors', 'defense', 'healthcare']

  -- Signal quality scoring (0.0 to 1.0)
  signal_score numeric(4,3) DEFAULT 0.05 CHECK (signal_score BETWEEN 0 AND 1),
  leadership_bonus numeric(4,3) DEFAULT 0.05,
  committee_bonus numeric(4,3) DEFAULT 0.0,
  alpha_bonus numeric(4,3) DEFAULT 0.0,

  -- Historical performance
  trailing_12m_return_pct numeric(6,2),
  trailing_12m_vs_spy_pct numeric(6,2),
  total_trades_ytd integer DEFAULT 0,
  win_rate_pct numeric(5,2),
  avg_days_to_file numeric(5,1),  -- average filing lag in days
  chronic_late_filer boolean DEFAULT false,  -- consistently files 35+ days late
  last_trade_date date,
  last_disclosure_date date,

  -- Spouse/family trade handling
  tracks_spouse boolean DEFAULT false,
  spouse_name text,

  notes text,
  data_source text DEFAULT 'quiverquant',
  updated_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_politician_intel_score ON public.politician_intel(signal_score DESC);
CREATE INDEX idx_politician_intel_name ON public.politician_intel(full_name text_pattern_ops);
CREATE INDEX idx_politician_intel_chamber ON public.politician_intel(chamber, signal_score DESC);
CREATE INDEX idx_politician_intel_late ON public.politician_intel(chronic_late_filer) WHERE chronic_late_filer = true;

ALTER TABLE public.politician_intel ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role manages politician_intel" ON public.politician_intel FOR ALL USING (true) WITH CHECK (true);

-- Table 2: legislative_calendar — upcoming votes and hearings by sector
CREATE TABLE public.legislative_calendar (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  event_date date NOT NULL,
  event_type text NOT NULL CHECK (event_type IN ('floor_vote', 'committee_hearing', 'markup_session', 'conference', 'recess')),
  chamber text CHECK (chamber IN ('house', 'senate', 'joint')),
  committee text,
  bill_id text,
  bill_title text,
  affected_sectors text[] DEFAULT '{}',
  affected_tickers text[] DEFAULT '{}',
  description text,
  significance text CHECK (significance IN ('low', 'medium', 'high', 'critical')),
  source_url text,
  fetched_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_leg_calendar_date ON public.legislative_calendar(event_date ASC);
CREATE INDEX idx_leg_calendar_sector ON public.legislative_calendar USING GIN(affected_sectors);
CREATE INDEX idx_leg_calendar_ticker ON public.legislative_calendar USING GIN(affected_tickers);

ALTER TABLE public.legislative_calendar ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role manages legislative_calendar" ON public.legislative_calendar FOR ALL USING (true) WITH CHECK (true);

-- Table 3: congress_clusters — detected multi-member buy clusters
CREATE TABLE public.congress_clusters (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  ticker text NOT NULL,
  cluster_date date NOT NULL,
  member_count integer NOT NULL DEFAULT 0,
  cross_chamber boolean DEFAULT false,
  members jsonb DEFAULT '[]'::jsonb,  -- [{name, chamber, party, signal_score, trade_date, disclosure_date}]
  avg_signal_score numeric(4,3),
  total_trade_value_range text,
  legislative_context text,
  confidence_boost numeric(4,3) DEFAULT 0.05,
  catalyst_event_ids uuid[] DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_congress_clusters_ticker ON public.congress_clusters(ticker, cluster_date DESC);
CREATE INDEX idx_congress_clusters_date ON public.congress_clusters(cluster_date DESC);

ALTER TABLE public.congress_clusters ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role manages congress_clusters" ON public.congress_clusters FOR ALL USING (true) WITH CHECK (true);

-- Extend catalyst_events: add politician scoring fields
ALTER TABLE public.catalyst_events
  ADD COLUMN IF NOT EXISTS politician_signal_score numeric(4,3),
  ADD COLUMN IF NOT EXISTS disclosure_days_since_trade integer,
  ADD COLUMN IF NOT EXISTS disclosure_freshness_score numeric(4,3),
  ADD COLUMN IF NOT EXISTS politician_bioguide_id text,
  ADD COLUMN IF NOT EXISTS filer_type text CHECK (filer_type IS NULL OR filer_type IN ('member', 'spouse', 'dependent_child')),
  ADD COLUMN IF NOT EXISTS in_jurisdiction boolean,
  ADD COLUMN IF NOT EXISTS congress_cluster_id uuid REFERENCES public.congress_clusters(id) ON DELETE SET NULL;

-- Seed the CONGRESS_MIRROR strategy profile
INSERT INTO public.strategy_profiles (
  profile_name,
  description,
  active,
  min_signal_score,
  min_tumbler_depth,
  min_confidence,
  max_risk_per_trade_pct,
  max_concurrent_positions,
  max_portfolio_risk_pct,
  position_size_method,
  trade_style,
  max_hold_days,
  circuit_breakers_enabled,
  self_modify_enabled,
  self_modify_requires_approval,
  annual_target_pct,
  daily_target_pct,
  prefer_high_beta
) VALUES (
  'CONGRESS_MIRROR',
  'Follows high-signal congressional disclosures. Targets leadership-tier members on policy-relevant committees. Scores trades by politician rank, disclosure freshness, legislative context, cluster patterns, and sector jurisdiction.',
  false,
  3,
  2,
  0.55,
  3.0,
  5,
  15.0,
  'fixed_pct',
  'swing',
  20,
  true,
  false,
  true,
  35.0,
  0.15,
  false
);
