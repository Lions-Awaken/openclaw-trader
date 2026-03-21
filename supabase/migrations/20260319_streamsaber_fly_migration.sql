-- StreamSaber Fly.io Migration
-- Creates tiktok_stream_events table for raw event storage with 30-day retention

-- Raw events table (high-value events only, 30-day retention)
CREATE TABLE public.tiktok_stream_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tiktok_account_id uuid NOT NULL REFERENCES public.tiktok_accounts(id) ON DELETE CASCADE,
  stream_id text NOT NULL,
  event_type text NOT NULL,  -- 'comment', 'gift', 'follow', 'subscribe', 'share'
  event_time timestamptz NOT NULL,
  event_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_stream_events_lookup ON public.tiktok_stream_events(tiktok_account_id, stream_id, event_time);
CREATE INDEX idx_stream_events_created ON public.tiktok_stream_events(created_at);

-- RLS
ALTER TABLE public.tiktok_stream_events ENABLE ROW LEVEL SECURITY;

-- Service role full access
CREATE POLICY "Service role manages events" ON public.tiktok_stream_events
  FOR ALL USING (true) WITH CHECK (true);

-- Admins can read events
CREATE POLICY "Admins read events" ON public.tiktok_stream_events
  FOR SELECT USING (
    EXISTS (SELECT 1 FROM public.users WHERE id = auth.uid() AND is_admin = true)
  );

-- Enable pg_cron for scheduled cleanup
CREATE EXTENSION IF NOT EXISTS pg_cron WITH SCHEMA pg_catalog;
GRANT USAGE ON SCHEMA cron TO postgres;

-- pg_cron: purge events older than 30 days (run daily at 3am UTC)
SELECT cron.schedule(
  'purge-old-stream-events',
  '0 3 * * *',
  $$DELETE FROM public.tiktok_stream_events WHERE created_at < now() - interval '30 days'$$
);
