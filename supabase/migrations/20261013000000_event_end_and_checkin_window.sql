-- Event end and check-in window (decisions 2026-10-12).
--
-- Check-in opens at `checkin_opens_at` (the start, unless the creator scheduled another
-- time or pressed "open now") and closes at `ends_at`, or earlier if the creator
-- closed it (`checkin_closed_at`). Places keep these null: their check-in follows the
-- opening hours (see app/services/checkin_window.py).
--
-- No DROP: three new nullable columns and a backfill.

alter table public.events
  add column if not exists ends_at timestamptz,
  add column if not exists checkin_opens_at timestamptz,
  add column if not exists checkin_closed_at timestamptz;

comment on column public.events.ends_at is
  'Fim do evento: o check-in fecha a esta hora. Importados sem fim da fonte: início + 4 h.';
comment on column public.events.checkin_opens_at is
  'Quando o check-in abre: o início por omissão, ou a hora programada/"abrir agora" do criador.';
comment on column public.events.checkin_closed_at is
  'O criador fechou o check-in à mão a esta hora (null = fecha no fim).';

-- Rows from before this migration did not store an end: the Sympla end was dropped
-- on import, and events created in the app had none. Four hours until a re-import or
-- an edit says otherwise.
update public.events
   set ends_at = event_date + interval '4 hours'
 where event_date is not null
   and ends_at is null;

update public.events
   set checkin_opens_at = event_date
 where event_date is not null
   and checkin_opens_at is null;

alter table public.events
  add constraint events_ends_after_start
  check (ends_at is null or event_date is null or ends_at > event_date);

-- A descoberta deixa de esconder os eventos que já começaram: só os que já acabaram.
-- Mesma assinatura, por isso basta substituir o corpo.
create or replace function public.match_events(
  query_embedding extensions.vector(1536),
  user_lat double precision,
  user_lng double precision,
  radius_km double precision,
  kinds text[] default null,
  categories text[] default null,
  date_from timestamptz default null,
  date_to timestamptz default null,
  max_price numeric default null
)
returns table (id uuid, similarity real, distance_km double precision)
language sql
stable
set search_path = public, extensions
-- Tag própria ($fn$) e não $$: o SQL Editor do Supabase parte o script em
-- comandos e confunde-se com $$ seguidos.
as $fn$
  with nearby as (
    select e.id,
           e.event_date,
           e.event_embedding,
           -- Haversine, em km. O least() protege o asin de arredondamentos acima de 1.
           6371.0 * 2 * asin(least(1.0, sqrt(
               power(sin(radians(e.lat - user_lat) / 2), 2)
             + cos(radians(user_lat)) * cos(radians(e.lat))
               * power(sin(radians(e.long - user_lng) / 2), 2)
           ))) as distance_km
      from public.events e
     -- Um evento acaba no fim, não no início: o check-in abre no início, por isso um
     -- evento a decorrer tem de continuar a aparecer. Sem fim, conta o início.
     where (e.event_date is null or coalesce(e.ends_at, e.event_date) >= now())
       -- Pré-filtro barato por latitude (1 grau ~ 111 km) antes da distância exata.
       and e.lat between user_lat - radius_km / 111.0 and user_lat + radius_km / 111.0
       and (kinds is null or e.kind = any (kinds))
       and (categories is null or e.category = any (categories))
       -- Lugares não têm data: passam sempre. Com filtro, eventos sem data ficam de fora.
       and (e.kind = 'place' or date_from is null or e.event_date >= date_from)
       and (e.kind = 'place' or date_to is null or e.event_date <= date_to)
       -- Lugares não têm preço exato: passam sempre. Preço desconhecido fica de fora.
       and (e.kind = 'place' or max_price is null or e.price <= max_price)
  )
  select n.id,
         case when query_embedding is not null and n.event_embedding is not null
              then (1 - (n.event_embedding <=> query_embedding))::real
         end as similarity,
         n.distance_km
    from nearby n
   where n.distance_km <= radius_km
   order by
     -- Com vetor de pesquisa: primeiro os itens com vetor, por semelhança.
     (query_embedding is not null and n.event_embedding is null),
     case when query_embedding is not null then n.event_embedding <=> query_embedding end,
     -- Empate, itens sem vetor, ou sem vetor de pesquisa: o mais próximo no tempo.
     n.event_date asc nulls last,
     n.id
   limit 200;
$fn$;

-- Só o backend a chama (service_role). Ver a regra das funções no CLAUDE.md.
revoke execute on function public.match_events(extensions.vector, double precision, double precision, double precision, text[], text[], timestamptz, timestamptz, numeric)
  from public, anon, authenticated;
grant execute on function public.match_events(extensions.vector, double precision, double precision, double precision, text[], text[], timestamptz, timestamptz, numeric)
  to service_role;
