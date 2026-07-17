-- Onboarding preferences + sync anchor counter (Flyer chat pipeline).
-- Apply in Supabase SQL editor or via CLI. Backend uses service_role (RLS bypass).

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS onboarding_preferences_text TEXT,
  ADD COLUMN IF NOT EXISTS onboarding_preferences_hash TEXT,
  ADD COLUMN IF NOT EXISTS guide_schema_version INTEGER NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS interest_sync_count_after_onboarding INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN public.profiles.onboarding_preferences_text IS
  'Canonical pt-BR summary derived server-side after complete-onboarding; injected into chat (Option A).';
COMMENT ON COLUMN public.profiles.interest_sync_count_after_onboarding IS
  'Increments atomically on each successful POST /chat/sync; used with SYNC_ANCHOR_COUNT for blend weights.';

CREATE OR REPLACE FUNCTION public.increment_interest_sync_count(p_user_id UUID)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  new_count INTEGER;
BEGIN
  UPDATE public.profiles
  SET interest_sync_count_after_onboarding = interest_sync_count_after_onboarding + 1
  WHERE id = p_user_id
  RETURNING interest_sync_count_after_onboarding INTO new_count;

  IF new_count IS NULL THEN
    RETURN -1;
  END IF;
  RETURN new_count;
END;
$$;

REVOKE ALL ON FUNCTION public.increment_interest_sync_count(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.increment_interest_sync_count(UUID) TO service_role;
