-- Second factor by e-mail for password logins (decisions 2026-10-12).
--
-- A session opened with a password only reaches the API once the 6-digit code sent to
-- the account's e-mail has been verified (app/api/auth.py). This table records the
-- verified sessions (`session_id` from the Supabase JWT).
--
-- Backend only: RLS on and no grants for clients. Like every public table, nothing
-- reaches it with the publishable key; the backend uses service_role.

create table if not exists public.login_second_factor (
  session_id uuid primary key,
  user_id uuid not null,
  verified_at timestamptz not null default now()
);

create index if not exists login_second_factor_user_id_idx
  on public.login_second_factor (user_id);

alter table public.login_second_factor enable row level security;

revoke all on table public.login_second_factor from public, anon, authenticated;
grant select, insert, delete on table public.login_second_factor to service_role;

comment on table public.login_second_factor is
  'Sessões de login com senha que já validaram o código enviado por e-mail (2.º fator).';
