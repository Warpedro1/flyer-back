-- User taste documents (one per onboarding step) for semantic retrieval (RAG).
-- Migrated from the previous per-user Chroma store into Supabase pgvector.
-- Apply in Supabase SQL editor or via CLI. Backend uses service_role (RLS bypass).

-- vector already enabled (profiles.interest_embedding); IF NOT EXISTS is safe.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS public.user_taste_documents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  step_key TEXT NOT NULL,
  content TEXT NOT NULL,
  embedding VECTOR(1536) NOT NULL,           -- text-embedding-3-small (EMBEDDING_DIMENSIONS)
  source TEXT NOT NULL DEFAULT 'onboarding',
  extraction TEXT NOT NULL DEFAULT 'llm',
  guide_schema_version INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, step_key)                 -- idempotent upsert per step
);

COMMENT ON TABLE public.user_taste_documents IS
  'Per-step taste docs from onboarding, embedded for per-facet retrieval (chat RAG).';

CREATE INDEX IF NOT EXISTS user_taste_documents_user_id_idx
  ON public.user_taste_documents (user_id);
-- No ivfflat/hnsw index: queries filter by user_id (~6 rows) before ordering by
-- distance, so the btree above suffices. Add a global ANN index only if the table
-- grows to hold many docs per user.

ALTER TABLE public.user_taste_documents ENABLE ROW LEVEL SECURITY;

CREATE POLICY "user_taste_documents_select_own"
  ON public.user_taste_documents FOR SELECT
  USING (auth.uid() = user_id);

-- Similarity search scoped to one user (mirrors match_events / SECURITY DEFINER).
CREATE OR REPLACE FUNCTION public.match_user_taste_documents(
  p_user_id UUID,
  query_embedding VECTOR(1536),
  match_count INTEGER DEFAULT 4
)
RETURNS TABLE (step_key TEXT, content TEXT, similarity REAL)
LANGUAGE sql
STABLE
SECURITY DEFINER
-- 'extensions' must be in scope: pgvector (and its <=> operator) lives there on Supabase.
SET search_path = public, extensions
AS $$
  SELECT d.step_key, d.content, (1 - (d.embedding <=> query_embedding))::real AS similarity
  FROM public.user_taste_documents d
  WHERE d.user_id = p_user_id
  ORDER BY d.embedding <=> query_embedding
  LIMIT GREATEST(match_count, 1);
$$;

REVOKE ALL ON FUNCTION public.match_user_taste_documents(UUID, VECTOR, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.match_user_taste_documents(UUID, VECTOR, INTEGER) TO service_role;
