-- Error records kept 30 days (#25 phase 1, decisions 2026-10-16).
--
-- Vercel Hobby keeps runtime logs for 1 hour: the onboarding failure of #26 could no
-- longer be investigated. The backend records here every 5xx, AI failure (moderation,
-- LLM judge) and error caught by the app's error screen (#33), with the `request_id`
-- the user sees as "ref".
--
-- No personal data: the user is a hash (sha256 of the id, 16 hex), and messages go
-- through `app/core/log_scrub.py` (no e-mail, CPF or phone) before they get here.
--
-- Backend only: RLS on and no grants for clients, like every public table.

create table if not exists public.app_errors (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  request_id text,
  source text not null check (source in ('api', 'ai', 'client')),
  method text,
  route text,
  status integer,
  code text,
  error_type text,
  message text check (char_length(message) <= 2000),
  user_hash text,
  details jsonb
);

create index if not exists app_errors_created_at_idx on public.app_errors (created_at);
create index if not exists app_errors_request_id_idx on public.app_errors (request_id);

alter table public.app_errors enable row level security;

revoke all on table public.app_errors from public, anon, authenticated;
grant select, insert, delete on table public.app_errors to service_role;

-- Retention: each insert removes what is older than 30 days. Cheap with the index, and
-- it needs no scheduler (pg_cron is not installed in the project). Runs as the
-- inserting role (service_role), which may delete.
create or replace function public.purge_old_app_errors()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  delete from public.app_errors where created_at < now() - interval '30 days';
  return null;
end;
$$;

revoke all on function public.purge_old_app_errors() from public, anon, authenticated;

create or replace trigger app_errors_purge
  after insert on public.app_errors
  for each statement execute function public.purge_old_app_errors();

comment on table public.app_errors is
  'Erros da API, da IA e da app (5xx, moderação, juiz), guardados 30 dias, sem dados pessoais.';
