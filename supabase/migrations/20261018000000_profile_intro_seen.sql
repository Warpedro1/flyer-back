-- Introduction tour seen (Warpedro1/flyer#23, decisions 2026-10-17).
--
-- The app shows a short tour on the first login and stores here when it was finished
-- or skipped, so it is not shown again on another device or browser.
--
-- Accounts that exist before the tour never see it: they are marked as seen now.
-- Sign-ups still waiting for their code are new people, so they keep it.

alter table public.profiles
  add column if not exists intro_seen_at timestamptz;

comment on column public.profiles.intro_seen_at is
  'Quando a pessoa terminou ou pulou a introdução do app (Warpedro1/flyer#23). Nulo = ainda não viu.';

update public.profiles
   set intro_seen_at = now()
 where intro_seen_at is null
   and signup_pending is not true;
