-- Public identity: @username, nickname ("vulgo") and profile photo (#32, #24; decisions
-- 2026-11-01).
--
-- What other people see of someone is their @username, nickname and photo; the full
-- name stays private. Every profile gets a unique @username made from the full name
-- (`pedro.guerra`, with digits when taken), the accounts that already existed too; the
-- person changes it later (once every 30 days, enforced by the backend through
-- `username_changed_at`). The @username and nickname chosen at sign-up are written by
-- the backend after its moderation, never taken from the sign-up metadata: whoever
-- calls Supabase Auth directly with the publishable key would skip it.
--
-- `profiles.email` now follows the e-mail of the account in Auth, which the e-mail
-- change of "Meus dados" relies on. Backend only: no grants for clients. No DROP.

alter table public.profiles
  add column if not exists username text,
  add column if not exists username_changed_at timestamptz,
  add column if not exists nickname text,
  add column if not exists avatar_path text;

comment on column public.profiles.username is
  'O @usuário público: único, 3 a 20 caracteres [a-z0-9._], sem palavras reservadas.';
comment on column public.profiles.username_changed_at is
  'Última troca do @usuário feita pela pessoa (nulo = nunca trocou); a próxima só 30 dias depois.';
comment on column public.profiles.nickname is
  'O vulgo (como as pessoas te conhecem): opcional, até 40 caracteres, moderado pelo backend.';
comment on column public.profiles.avatar_path is
  'Foto de perfil no bucket avatars ({uid}/{uuid}.ext); lida só por URL assinada.';

-- A name is reserved when any of its pieces between dots, underscores and digits is one
-- of these words (`flyer1`, `admin_br`). Kept equal to app/core/usernames.py by a test.
-- Tag própria ($fn$) e não $$: o SQL Editor do Supabase parte o script em comandos.
create or replace function public.is_reserved_username(value text)
returns boolean
language sql
immutable
set search_path = ''
as $fn$
  select exists (
    select 1
      from regexp_split_to_table(lower(coalesce(value, '')), '[._0-9]+') as piece
     where piece = any (array[
       'admin', 'flyer', 'suporte', 'ajuda', 'contato', 'oficial', 'equipe', 'moderacao',
       'sistema', 'seguranca', 'root', 'api', 'www', 'null'
     ]::text[])
  );
$fn$;

-- 3 to 20 of [a-z0-9._], not starting or ending with a dot, no "..", at least one letter,
-- at most 7 digits in a row (no phone or CPF fits) and not reserved.
create or replace function public.is_valid_username(value text)
returns boolean
language sql
immutable
set search_path = ''
as $fn$
  select value is not null
     and value ~ '^[a-z0-9_][a-z0-9._]{1,18}[a-z0-9_]$'
     and value !~ '\.\.'
     and value ~ '[a-z]'
     and value !~ '[0-9]{8}'
     and not public.is_reserved_username(value);
$fn$;

-- A free @username made from a full name: lower case, without accents, words joined by
-- dots, at most 15 characters, plus digits when taken (after a `_` when the name already
-- ends in digits). `usuario` when the name gives nothing usable (empty, too short, no
-- letter, only emoji or other scripts, reserved).
create or replace function public.generate_username(full_name text)
returns text
language plpgsql
volatile
set search_path = ''
as $fn$
declare
  base text;
  candidate text;
begin
  base := translate(
    lower(coalesce(full_name, '')),
    'áàâãäåéèêëíìîïóòôõöúùûüçñýÿ',
    'aaaaaaeeeeiiiiooooouuuucnyy'
  );
  base := btrim(regexp_replace(base, '[^a-z0-9]+', '.', 'g'), '.');
  base := rtrim(left(base, 15), '.');
  if not public.is_valid_username(base) then
    base := 'usuario';
  end if;

  candidate := base;
  for attempt in 1..30 loop
    if public.is_valid_username(candidate)
       and not exists (select 1 from public.profiles where username = candidate) then
      return candidate;
    end if;
    candidate := base
      || case when base ~ '[0-9]$' then '_' else '' end
      || case
           when attempt <= 10 then (10 + floor(random() * 90))::int::text
           else (1000 + floor(random() * 9000))::int::text
         end;
  end loop;
  return 'usuario' || lpad(floor(random() * 1000000)::int::text, 6, '0');
end;
$fn$;

create unique index if not exists profiles_username_key on public.profiles (username);

-- Any profile inserted without a username gets one: every new account (the Auth
-- trigger below) and the backend's get_or_create_profile, which inserts only the id.
create or replace function public.profiles_fill_username()
returns trigger
language plpgsql
set search_path = ''
as $fn$
begin
  if new.username is null then
    new.username := public.generate_username(new.name);
  end if;
  return new;
end;
$fn$;

create or replace trigger profiles_fill_username
  before insert on public.profiles
  for each row execute function public.profiles_fill_username();

-- Same body as 20261016000000; the username comes from profiles_fill_username. Two
-- sign-ups with the same name at once: the one whose username is taken while it waits
-- tries again and gets another. A clash on the CPF or the phone still fails the sign-up
-- (the backend answers 409).
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $fn$
declare
  free_plan_id uuid;
  meta jsonb := coalesce(new.raw_user_meta_data, '{}'::jsonb);
  violated text;
begin
  select id into free_plan_id from public.subscription_plans where name = 'Free' limit 1;

  for attempt in 1..5 loop
    begin
      insert into public.profiles (id, name, email, plan_id, cpf, phone, signup_pending)
      values (
        new.id,
        coalesce(meta->>'name', meta->>'full_name'),
        new.email,
        free_plan_id,
        nullif(meta->>'cpf', ''),
        nullif(meta->>'phone', ''),
        coalesce(meta->>'flyer_signup', '') = 'true'
      );
      exit;
    exception when unique_violation then
      get stacked diagnostics violated = constraint_name;
      if violated is distinct from 'profiles_username_key' or attempt = 5 then
        raise;
      end if;
    end;
  end loop;

  insert into public.ai_chats (user_id) values (new.id);

  return new;
end;
$fn$;

-- `profiles.email` follows the account's e-mail (the e-mail change confirms it in Auth).
create or replace function public.sync_profile_email()
returns trigger
language plpgsql
security definer
set search_path = ''
as $fn$
begin
  update public.profiles set email = new.email where id = new.id;
  return new;
end;
$fn$;

create or replace trigger on_auth_user_email_changed
  after update of email on auth.users
  for each row
  when (old.email is distinct from new.email)
  execute function public.sync_profile_email();

-- Accounts that already exist: a username from the full name, one at a time so that two
-- equal names get different ones, and the e-mail Auth has now (nothing kept them in step).
do $backfill$
declare
  r record;
begin
  for r in select id, name from public.profiles where username is null order by id loop
    update public.profiles set username = public.generate_username(r.name) where id = r.id;
  end loop;
end
$backfill$;

update public.profiles p
   set email = u.email
  from auth.users u
 where u.id = p.id
   and p.email is distinct from u.email;

alter table public.profiles
  alter column username set not null,
  add constraint profiles_username_valid check (public.is_valid_username(username)),
  add constraint profiles_nickname_length check (nickname is null or char_length(nickname) between 1 and 40),
  add constraint profiles_avatar_path_own check (
    avatar_path is null
    or avatar_path ~ '^[0-9a-f-]{36}/[0-9a-f-]{36}\.(jpg|png|webp)$'
  );

-- Only the backend calls them (service_role). See the functions rule in CLAUDE.md.
revoke execute on function public.is_reserved_username(text) from public, anon, authenticated;
revoke execute on function public.is_valid_username(text) from public, anon, authenticated;
revoke execute on function public.generate_username(text) from public, anon, authenticated;
revoke execute on function public.profiles_fill_username() from public, anon, authenticated;
revoke execute on function public.handle_new_user() from public, anon, authenticated;
revoke execute on function public.sync_profile_email() from public, anon, authenticated;
grant execute on function public.is_reserved_username(text) to service_role;
grant execute on function public.is_valid_username(text) to service_role;
grant execute on function public.generate_username(text) to service_role;
