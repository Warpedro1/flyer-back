-- CPF and mobile on every account (decisions 2026-10-12).
--
-- Unique per account and validated here as well as in the backend: whoever calls
-- Supabase Auth directly, skipping the backend, still cannot store a fake or repeated
-- CPF or phone. Only the owner ever sees them (masked); the API never returns them to
-- anyone else. Google accounts start without them and complete them on first access.
-- No DROP.

-- Tag própria ($fn$) e não $$: o SQL Editor do Supabase parte o script em comandos.
create or replace function public.is_valid_cpf(value text)
returns boolean
language plpgsql
immutable
set search_path = ''
as $fn$
declare
  digits int[];
  total int;
  check_digit int;
begin
  if value is null or value !~ '^[0-9]{11}$' or value ~ '^([0-9])\1{10}$' then
    return false;
  end if;
  digits := array(select substr(value, g, 1)::int from generate_series(1, 11) as g);
  total := 0;
  for i in 1..9 loop
    total := total + digits[i] * (11 - i);
  end loop;
  check_digit := (total * 10) % 11;
  if check_digit = 10 then check_digit := 0; end if;
  if check_digit <> digits[10] then
    return false;
  end if;
  total := 0;
  for i in 1..10 loop
    total := total + digits[i] * (12 - i);
  end loop;
  check_digit := (total * 10) % 11;
  if check_digit = 10 then check_digit := 0; end if;
  return check_digit = digits[11];
end;
$fn$;

revoke execute on function public.is_valid_cpf(text) from public, anon, authenticated;
grant execute on function public.is_valid_cpf(text) to service_role;

alter table public.profiles
  add column if not exists cpf text,
  add column if not exists phone text;

comment on column public.profiles.cpf is 'CPF (11 dígitos, sem pontuação). Único. Só o próprio o vê, mascarado.';
comment on column public.profiles.phone is 'Celular em E.164 (+55DD9XXXXXXXX). Único. Só o próprio o vê, mascarado.';

alter table public.profiles
  add constraint profiles_cpf_valid check (cpf is null or public.is_valid_cpf(cpf)),
  add constraint profiles_phone_valid check (phone is null or phone ~ '^\+55[1-9]{2}9[0-9]{8}$');

create unique index if not exists profiles_cpf_key on public.profiles (cpf) where cpf is not null;
create unique index if not exists profiles_phone_key on public.profiles (phone) where phone is not null;

-- The sign-up metadata (sent by the backend's POST /auth/signup) carries cpf and phone.
-- Google sends `name`/`full_name` only. Same body as before plus the two columns.
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

  insert into public.profiles (id, name, email, plan_id, cpf, phone)
  values (
    new.id,
    coalesce(new.raw_user_meta_data->>'name', new.raw_user_meta_data->>'full_name'),
    new.email,
    free_plan_id,
    nullif(new.raw_user_meta_data->>'cpf', ''),
    nullif(new.raw_user_meta_data->>'phone', '')
  );

  insert into public.ai_chats (user_id) values (new.id);

  return new;
end;
$fn$;

revoke execute on function public.handle_new_user() from public, anon, authenticated;
