-- Ingestion support for scraped events (ETL: Apify -> LLM -> upsert).
-- Apply in Supabase SQL editor or via CLI. Backend uses service_role for upserts.

-- Provenance columns used as the idempotent upsert key.
ALTER TABLE public.events
  ADD COLUMN IF NOT EXISTS source TEXT,
  ADD COLUMN IF NOT EXISTS source_event_id TEXT;

-- Idempotency: one row per (source, source_event_id). Partial so manually-created
-- events (source IS NULL) are unaffected and can coexist freely.
CREATE UNIQUE INDEX IF NOT EXISTS events_source_source_event_id_key
  ON public.events (source, source_event_id)
  WHERE source IS NOT NULL AND source_event_id IS NOT NULL;

COMMENT ON COLUMN public.events.source IS
  'Origin of the event (e.g. "apify", "mock"); NULL for user-created events.';
COMMENT ON COLUMN public.events.source_event_id IS
  'Stable id from the source; together with source it is the upsert conflict target.';

-- NOTE: discovery (match_events) is vector search, so ingested events must also carry
-- an embedding. This assumes public.events already has an `embedding vector(1536)`
-- column (same as profiles.interest_embedding / user_taste_documents.embedding). If it
-- does not exist yet, create it:
--   ALTER TABLE public.events ADD COLUMN IF NOT EXISTS embedding vector(1536);
