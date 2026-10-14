-- Sign-up confirmed by code, tied to the tab that asked for it (decision 2026-10-14).
--
-- The sign-up e-mail carries only a 6-digit code. Supabase codes can be redeemed with
-- the publishable key, so the code alone must not open the account: whoever gets the
-- e-mail by mistake (a mistyped address) would otherwise use an account carrying
-- someone else's CPF. The backend answers the sign-up with a secret ticket that stays
-- in the tab that signed up; the profile stays pending until the code and that ticket
-- are presented together (POST /auth/signup/activate). Pending accounts get 403
-- `signup_pending` on the API, and a new sign-up with the same e-mail/CPF/phone
-- replaces them.
--
-- Backend only, like every public table: no grants for clients. No DROP.

alter table public.profiles
  add column if not exists signup_pending boolean not null default false;

comment on column public.profiles.signup_pending is
  'Cadastro por e-mail à espera do código no separador que o fez (bilhete em signup_tickets).';

create table if not exists public.signup_tickets (
  user_id uuid primary key references auth.users (id) on delete cascade,
  ticket_hash text not null,
  created_at timestamptz not null default now()
);

alter table public.signup_tickets enable row level security;

revoke all on table public.signup_tickets from public, anon, authenticated;
grant select, insert, update, delete on table public.signup_tickets to service_role;

comment on table public.signup_tickets is
  'sha256 do bilhete entregue ao separador que fez o cadastro; apagado ao ativar a conta.';

-- Same body as 20261015000000 plus signup_pending: set by the backend's sign-up
-- (`flyer_signup` in the metadata); Google accounts start active.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $fn$
declare
  free_plan_id uuid;
begin
  select id into free_plan_id from public.subscription_plans where name = 'Free' limit 1;

  insert into public.profiles (id, name, email, plan_id, cpf, phone, signup_pending)
  values (
    new.id,
    coalesce(new.raw_user_meta_data->>'name', new.raw_user_meta_data->>'full_name'),
    new.email,
    free_plan_id,
    nullif(new.raw_user_meta_data->>'cpf', ''),
    nullif(new.raw_user_meta_data->>'phone', ''),
    coalesce(new.raw_user_meta_data->>'flyer_signup', '') = 'true'
  );

  insert into public.ai_chats (user_id) values (new.id);

  return new;
end;
$fn$;

revoke execute on function public.handle_new_user() from public, anon, authenticated;
