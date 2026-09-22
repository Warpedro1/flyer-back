-- Lotação por evento, lista de espera FIFO e admissão por QR code.
-- Aplicar no SQL editor do Supabase ou via CLI.
--
-- Porque é que as escritas da fila vivem todas em funções e não no PostgREST:
-- o backend fala com o Supabase por REST, que não dá transações multi-statement.
-- "contar os confirmados e decidir se ainda há vaga" em dois pedidos deixa duas
-- pessoas passarem a última vaga ao mesmo tempo. As funções abaixo trancam a
-- linha do evento (SELECT ... FOR UPDATE) e fazem a decisão inteira numa
-- transação só, no mesmo padrão de public.increment_interest_sync_count.

-- ---------------------------------------------------------------- 1. evento --

alter table public.events
  add column if not exists capacity integer
    check (capacity is null or capacity > 0),
  add column if not exists waitlist_enabled boolean not null default true,
  add column if not exists call_ttl_minutes integer not null default 10
    check (call_ttl_minutes between 1 and 240),
  add column if not exists auto_call_next boolean not null default true;

comment on column public.events.capacity is
  'Lotação máxima. NULL = evento sem limite de pessoas (a fila nunca é usada).';
comment on column public.events.waitlist_enabled is
  'Com a lotação cheia: true põe em lista de espera, false recusa a inscrição.';
comment on column public.events.call_ttl_minutes is
  'Minutos que a pessoa chamada tem para aparecer antes de passar a no_show.';
comment on column public.events.auto_call_next is
  'Quando uma chamada expira, chama o próximo da fila automaticamente.';

-- ------------------------------------------------------------- 2. inscrições --

do $$ begin
  create type public.rsvp_status as enum
    ('confirmed','waitlisted','called','admitted','no_show','cancelled');
exception when duplicate_object then null; end $$;

create table if not exists public.event_rsvps (
  id                uuid primary key default gen_random_uuid(),
  event_id          uuid not null references public.events(id) on delete cascade,
  user_id           uuid not null references public.profiles(id) on delete cascade,
  status            public.rsvp_status not null default 'confirmed',
  waitlist_position integer,
  called_at         timestamptz,
  call_expires_at   timestamptz,
  admitted_at       timestamptz,
  no_show_count     integer not null default 0,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  unique (event_id, user_id)
);

comment on table public.event_rsvps is
  'Uma linha por (evento, utilizador). Escrita apenas pelas funções rsvp_*.';

-- Ordem da fila.
create index if not exists event_rsvps_queue_idx
  on public.event_rsvps (event_id, waitlist_position)
  where status = 'waitlisted';
-- Varredura de chamadas vencidas sem sequential scan.
create index if not exists event_rsvps_expiring_idx
  on public.event_rsvps (call_expires_at)
  where status = 'called';
create index if not exists event_rsvps_user_idx
  on public.event_rsvps (user_id);

alter table public.event_rsvps enable row level security;

-- O participante lê a própria inscrição...
drop policy if exists "rsvp_select_own" on public.event_rsvps;
create policy "rsvp_select_own" on public.event_rsvps
  for select to authenticated using (user_id = auth.uid());

-- ...e o criador vê a lista toda do seu evento.
drop policy if exists "rsvp_select_as_creator" on public.event_rsvps;
create policy "rsvp_select_as_creator" on public.event_rsvps
  for select to authenticated using (
    exists (select 1 from public.events e
            where e.id = event_id and e.creator_id = auth.uid())
  );

-- Sem policies de INSERT/UPDATE/DELETE de propósito: escrever a fila à mão
-- contornaria as travas de concorrência das funções abaixo.

-- -------------------------------------------------- 3. QR: nonces de uso único --

create table if not exists public.rsvp_scan_nonces (
  jti        text primary key,
  rsvp_id    uuid not null references public.event_rsvps(id) on delete cascade,
  scanned_at timestamptz not null default now()
);

comment on table public.rsvp_scan_nonces is
  'Um jti por leitura de QR. O INSERT falhar com 23505 É a deteção de replay.';

create index if not exists rsvp_scan_nonces_scanned_at_idx
  on public.rsvp_scan_nonces (scanned_at);

alter table public.rsvp_scan_nonces enable row level security;
-- Sem policies: só o service_role (backend) lá toca.

-- ------------------------------------------------------- 4. contagem de vagas --

-- Ocupação = confirmed + called + admitted. waitlisted/no_show/cancelled não ocupam.
create or replace function public.rsvp_taken_count(p_event_id uuid)
returns integer
language sql stable security definer set search_path = public as $$
  select count(*)::int from public.event_rsvps
   where event_id = p_event_id
     and status in ('confirmed','called','admitted');
$$;

-- Mantém events.attendee_count (coluna que já existia e já é servida pela API)
-- alinhado com a ocupação, para não obrigar a um count por leitura de evento.
create or replace function public.rsvp_sync_attendee_count()
returns trigger
language plpgsql security definer set search_path = public as $$
declare
  v_event_id uuid := coalesce(new.event_id, old.event_id);
begin
  update public.events
     set attendee_count = public.rsvp_taken_count(v_event_id)
   where id = v_event_id;
  return null;
end $$;

drop trigger if exists rsvp_attendee_count_trg on public.event_rsvps;
create trigger rsvp_attendee_count_trg
  after insert or delete or update of status on public.event_rsvps
  for each row execute function public.rsvp_sync_attendee_count();

-- --------------------------------------------------------- 5. varredura FIFO --

-- Expira chamadas vencidas e, se o evento o permitir, chama já o próximo da fila
-- por cada vaga libertada — é isto que impede uma pessoa que não aparece de
-- bloquear a fila. Idempotente: sem nada vencido não faz nada e devolve 0.
--
-- Não há scheduler neste backend, por isso a expiração é preguiçosa: toda a
-- operação que toca na fila chama esta função primeiro. O ecrã do criador faz
-- polling, o que na prática mantém a fila a andar sozinha.
create or replace function public.rsvp_sweep_expired_calls(p_event_id uuid)
returns integer
language plpgsql security definer set search_path = public as $$
declare
  v_auto  boolean;
  v_ttl   integer;
  v_freed integer;
  v_next  uuid;
begin
  select auto_call_next, call_ttl_minutes into v_auto, v_ttl
    from public.events where id = p_event_id for update;
  if not found then
    return 0;
  end if;

  with expired as (
    update public.event_rsvps
       set status            = 'no_show',
           no_show_count     = no_show_count + 1,
           waitlist_position = null,
           updated_at        = now()
     where event_id = p_event_id
       and status   = 'called'
       and call_expires_at < now()
    returning 1
  )
  select count(*)::int into v_freed from expired;

  if v_freed = 0 or not v_auto then
    return v_freed;
  end if;

  for _i in 1..v_freed loop
    select id into v_next
      from public.event_rsvps
     where event_id = p_event_id and status = 'waitlisted'
     order by waitlist_position
     limit 1;
    exit when v_next is null;   -- fila vazia

    update public.event_rsvps
       set status            = 'called',
           called_at         = now(),
           call_expires_at   = now() + make_interval(mins => v_ttl),
           waitlist_position = null,
           updated_at        = now()
     where id = v_next;
    v_next := null;
  end loop;

  return v_freed;
end $$;

-- ---------------------------------------------------------- 6. entrar na fila --

-- Confirma se há vaga, senão põe no fim da lista de espera. Reentrante: quem já
-- está inscrito recebe a própria linha de volta sem efeitos secundários.
create or replace function public.rsvp_join(p_event_id uuid, p_user_id uuid)
returns public.event_rsvps
language plpgsql security definer set search_path = public as $$
declare
  v_capacity int;
  v_waitlist boolean;
  v_taken    int;
  v_next_pos int;
  v_row      public.event_rsvps;
begin
  perform public.rsvp_sweep_expired_calls(p_event_id);

  -- Trava a linha do evento: serializa quem disputa a última vaga.
  select capacity, waitlist_enabled into v_capacity, v_waitlist
    from public.events where id = p_event_id for update;
  if not found then
    raise exception 'event_not_found' using errcode = 'P0002';
  end if;

  select * into v_row from public.event_rsvps
   where event_id = p_event_id and user_id = p_user_id;
  if found and v_row.status not in ('cancelled','no_show') then
    return v_row;
  end if;

  v_taken := public.rsvp_taken_count(p_event_id);

  -- capacity IS NULL = evento sem limite: entra sempre como confirmado.
  if v_capacity is null or v_taken < v_capacity then
    insert into public.event_rsvps (event_id, user_id, status)
         values (p_event_id, p_user_id, 'confirmed')
    on conflict (event_id, user_id) do update
       set status = 'confirmed', waitlist_position = null, updated_at = now()
    returning * into v_row;

  elsif v_waitlist then
    select coalesce(max(waitlist_position), 0) + 1 into v_next_pos
      from public.event_rsvps where event_id = p_event_id;

    insert into public.event_rsvps (event_id, user_id, status, waitlist_position)
         values (p_event_id, p_user_id, 'waitlisted', v_next_pos)
    on conflict (event_id, user_id) do update
       set status = 'waitlisted', waitlist_position = v_next_pos, updated_at = now()
    returning * into v_row;

  else
    raise exception 'event_full' using errcode = 'P0001';
  end if;

  return v_row;
end $$;

-- ------------------------------------------------------------- 7. desistência --

-- Sair liberta a vaga, e a vaga libertada promove já o primeiro da fila.
create or replace function public.rsvp_cancel(p_event_id uuid, p_user_id uuid)
returns public.event_rsvps
language plpgsql security definer set search_path = public as $$
declare
  v_row      public.event_rsvps;
  v_occupied boolean;
  v_capacity int;
  v_next     uuid;
begin
  select capacity into v_capacity
    from public.events where id = p_event_id for update;
  if not found then
    raise exception 'event_not_found' using errcode = 'P0002';
  end if;

  select * into v_row from public.event_rsvps
   where event_id = p_event_id and user_id = p_user_id;
  if not found then
    return null;
  end if;

  v_occupied := v_row.status in ('confirmed','called','admitted');

  update public.event_rsvps
     set status = 'cancelled', waitlist_position = null, updated_at = now()
   where id = v_row.id
  returning * into v_row;

  -- Só faz sentido promover se a saída libertou mesmo uma vaga contada.
  if v_occupied and v_capacity is not null then
    select id into v_next
      from public.event_rsvps
     where event_id = p_event_id and status = 'waitlisted'
     order by waitlist_position
     limit 1;

    if v_next is not null then
      update public.event_rsvps
         set status = 'confirmed', waitlist_position = null, updated_at = now()
       where id = v_next;
    end if;
  end if;

  return v_row;
end $$;

-- -------------------------------------------------------- 8. chamar o próximo --

create or replace function public.rsvp_call_next(p_event_id uuid, p_creator_id uuid)
returns public.event_rsvps
language plpgsql security definer set search_path = public as $$
declare
  v_creator uuid;
  v_ttl     int;
  v_next    uuid;
  v_row     public.event_rsvps;
begin
  perform public.rsvp_sweep_expired_calls(p_event_id);

  select creator_id, call_ttl_minutes into v_creator, v_ttl
    from public.events where id = p_event_id for update;
  if not found then
    raise exception 'event_not_found' using errcode = 'P0002';
  end if;
  if v_creator is distinct from p_creator_id then
    raise exception 'not_event_creator' using errcode = 'P0003';
  end if;

  select id into v_next
    from public.event_rsvps
   where event_id = p_event_id and status = 'waitlisted'
   order by waitlist_position
   limit 1;
  if v_next is null then
    return null;   -- fila vazia
  end if;

  update public.event_rsvps
     set status            = 'called',
         called_at         = now(),
         call_expires_at   = now() + make_interval(mins => v_ttl),
         waitlist_position = null,
         updated_at        = now()
   where id = v_next
  returning * into v_row;

  return v_row;
end $$;

-- ------------------------------------------------------------- 9. rechamar --

-- A pessoa chegou atrasada depois do prazo: devolve-lhe uma chamada nova.
create or replace function public.rsvp_recall(
  p_event_id uuid, p_rsvp_id uuid, p_creator_id uuid
)
returns public.event_rsvps
language plpgsql security definer set search_path = public as $$
declare
  v_creator uuid;
  v_ttl     int;
  v_row     public.event_rsvps;
begin
  select creator_id, call_ttl_minutes into v_creator, v_ttl
    from public.events where id = p_event_id for update;
  if not found then
    raise exception 'event_not_found' using errcode = 'P0002';
  end if;
  if v_creator is distinct from p_creator_id then
    raise exception 'not_event_creator' using errcode = 'P0003';
  end if;

  update public.event_rsvps
     set status          = 'called',
         called_at       = now(),
         call_expires_at = now() + make_interval(mins => v_ttl),
         updated_at      = now()
   where id = p_rsvp_id and event_id = p_event_id
     and status in ('no_show','waitlisted')
  returning * into v_row;

  if not found then
    raise exception 'rsvp_not_recallable' using errcode = 'P0004';
  end if;

  return v_row;
end $$;

-- ------------------------------------------------------------ 10. admissão --

-- Marca a entrada. O backend já validou a assinatura e a validade do token;
-- aqui garante-se o resto de forma atómica (estado elegível + jti de uso único).
create or replace function public.rsvp_admit(
  p_rsvp_id uuid, p_event_id uuid, p_jti text
)
returns public.event_rsvps
language plpgsql security definer set search_path = public as $$
declare
  v_row public.event_rsvps;
begin
  select * into v_row from public.event_rsvps
   where id = p_rsvp_id and event_id = p_event_id
     for update;
  if not found then
    raise exception 'rsvp_not_found' using errcode = 'P0002';
  end if;

  if v_row.status = 'admitted' then
    raise exception 'already_admitted' using errcode = 'P0005';
  end if;
  if v_row.status not in ('confirmed','called') then
    raise exception 'rsvp_not_admissible' using errcode = 'P0006';
  end if;

  -- Uso único: a violação de unicidade aqui é a deteção de replay do QR.
  insert into public.rsvp_scan_nonces (jti, rsvp_id) values (p_jti, v_row.id);

  update public.event_rsvps
     set status            = 'admitted',
         admitted_at       = now(),
         waitlist_position = null,
         updated_at        = now()
   where id = v_row.id
  returning * into v_row;

  return v_row;
end $$;

-- --------------------------------------------------------------- 11. grants --
--
-- ATENÇÃO: os revokes abaixo NÃO bastam. O Supabase concede EXECUTE a anon e
-- authenticated por ALTER DEFAULT PRIVILEGES quando a função é criada, e isso é
-- separado do role PUBLIC — estas funções ficaram chamáveis com a chave
-- publicável. Corrigido em 20260922000000_rsvp_revoke_execute_from_client_roles,
-- que revoga explicitamente desses dois roles. Ver lá o detalhe.

revoke all on function public.rsvp_taken_count(uuid)                  from public;
revoke all on function public.rsvp_sweep_expired_calls(uuid)          from public;
revoke all on function public.rsvp_join(uuid, uuid)                   from public;
revoke all on function public.rsvp_cancel(uuid, uuid)                 from public;
revoke all on function public.rsvp_call_next(uuid, uuid)              from public;
revoke all on function public.rsvp_recall(uuid, uuid, uuid)           from public;
revoke all on function public.rsvp_admit(uuid, uuid, text)            from public;

grant execute on function public.rsvp_taken_count(uuid)               to service_role;
grant execute on function public.rsvp_sweep_expired_calls(uuid)       to service_role;
grant execute on function public.rsvp_join(uuid, uuid)                to service_role;
grant execute on function public.rsvp_cancel(uuid, uuid)              to service_role;
grant execute on function public.rsvp_call_next(uuid, uuid)           to service_role;
grant execute on function public.rsvp_recall(uuid, uuid, uuid)        to service_role;
grant execute on function public.rsvp_admit(uuid, uuid, text)         to service_role;
