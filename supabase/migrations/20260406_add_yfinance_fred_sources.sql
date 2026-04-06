-- Add yfinance and fred as catalyst_events sources
-- Add fundamental_shift as a catalyst type

-- Drop and recreate source CHECK constraint
ALTER TABLE catalyst_events DROP CONSTRAINT IF EXISTS catalyst_events_source_check;
ALTER TABLE catalyst_events ADD CONSTRAINT catalyst_events_source_check
  CHECK (source = ANY (ARRAY[
    'finnhub'::text, 'perplexity'::text, 'sec_edgar'::text,
    'quiverquant'::text, 'manual'::text, 'yfinance'::text, 'fred'::text
  ]));

-- Drop and recreate catalyst_type CHECK constraint (add fundamental_shift)
ALTER TABLE catalyst_events DROP CONSTRAINT IF EXISTS catalyst_events_catalyst_type_check;
ALTER TABLE catalyst_events ADD CONSTRAINT catalyst_events_catalyst_type_check
  CHECK (catalyst_type = ANY (ARRAY[
    'earnings_surprise'::text, 'analyst_action'::text, 'insider_transaction'::text,
    'congressional_trade'::text, 'sec_filing'::text, 'executive_social'::text,
    'influencer_endorsement'::text, 'government_contract'::text, 'product_launch'::text,
    'regulatory_action'::text, 'macro_event'::text, 'sector_rotation'::text,
    'supply_chain'::text, 'partnership'::text, 'fundamental_shift'::text, 'other'::text
  ]));
