-- =============================================================================
-- Fix: order_events CHECK constraint + trade_decisions missing columns
--
-- Issue 1: order_events event_type CHECK only allowed 7 values but the code
--   writes 'poll_timeout', 'partially_filled', and 'done_for_day'. Drop the
--   old constraint and replace with the full 10-value set.
--
-- Issue 2: trade_decisions table was missing 12 columns that scanner.py writes
--   on every trade: decision, confidence, qty, side, stop_price, trade_style,
--   inference_chain_id, entry_order_id, stop_order_id, profile_name,
--   signals_score, max_depth_reached.
--   Without these the scanner's _post_to_supabase call returned HTTP 400 /
--   PGRST204 and no trade record was ever persisted.
--
-- Applied: vpollvsbtushbiapoflr — 2026-03-30
-- =============================================================================

-- ----------------------------------------------------------------------------
-- Issue 1: Expand order_events event_type CHECK constraint
-- ----------------------------------------------------------------------------

ALTER TABLE public.order_events
  DROP CONSTRAINT IF EXISTS order_events_event_type_check;

ALTER TABLE public.order_events
  ADD CONSTRAINT order_events_event_type_check
  CHECK (event_type = ANY (ARRAY[
    'submitted',
    'filled',
    'partial_fill',
    'partially_filled',
    'rejected',
    'cancelled',
    'expired',
    'replaced',
    'poll_timeout',
    'done_for_day'
  ]));

-- ----------------------------------------------------------------------------
-- Issue 2: Add missing columns to trade_decisions
-- ----------------------------------------------------------------------------

ALTER TABLE public.trade_decisions
  ADD COLUMN IF NOT EXISTS decision          text,
  ADD COLUMN IF NOT EXISTS confidence        numeric(6,4),
  ADD COLUMN IF NOT EXISTS qty               numeric(12,4),
  ADD COLUMN IF NOT EXISTS side              text,
  ADD COLUMN IF NOT EXISTS stop_price        numeric(12,4),
  ADD COLUMN IF NOT EXISTS trade_style       text,
  ADD COLUMN IF NOT EXISTS inference_chain_id uuid
    REFERENCES public.inference_chains(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS entry_order_id    text,
  ADD COLUMN IF NOT EXISTS stop_order_id     text,
  ADD COLUMN IF NOT EXISTS profile_name      text,
  ADD COLUMN IF NOT EXISTS signals_score     numeric(6,4),
  ADD COLUMN IF NOT EXISTS max_depth_reached integer;

-- Index the FK for join performance (scanner and dashboard both query by
-- inference_chain_id to link trade decisions back to the tumbler chain)
CREATE INDEX IF NOT EXISTS idx_trade_decisions_inference_chain
  ON public.trade_decisions(inference_chain_id)
  WHERE inference_chain_id IS NOT NULL;

-- Index profile_name for dashboard economics tab filtering
CREATE INDEX IF NOT EXISTS idx_trade_decisions_profile
  ON public.trade_decisions(profile_name, created_at DESC)
  WHERE profile_name IS NOT NULL;

-- ----------------------------------------------------------------------------
-- Issue 3: Fix legacy NOT NULL columns that scanner does not write
--
-- trade_decisions.action and trade_decisions.content are legacy NOT NULL
-- columns from the old schema. The scanner never writes them (they are not in
-- the trade_decision dict in scanner.py). Without defaults, every scanner
-- insert fails with a NOT NULL violation.
--
-- action has a CHECK constraint (BUY, SELL, CLOSE, STOP_OUT, PARTIAL) so the
-- default must be one of those values. Scanner always enters long, so 'BUY'.
-- content is a free-text field with no CHECK; default to empty string.
-- ----------------------------------------------------------------------------

ALTER TABLE public.trade_decisions
  ALTER COLUMN action  SET DEFAULT 'BUY',
  ALTER COLUMN content SET DEFAULT '';
