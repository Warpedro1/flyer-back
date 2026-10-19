-- Content moderation of events and places (#22 part A, decisions 2026-10-18).
--
-- What is created in the app is checked before it is written (422 when refused),
-- so a stored row is always clean. What is imported is checked by the import, or
-- later by POST /admin/events/moderation for the rows that were here before:
-- an imported row that is refused is hidden, never shown in discovery.
--
-- `moderation_hash` is the md5 of the title, description and cover the check saw
-- (`moderation.event_moderation_hash`, same expression). `moderation_stale` compares
-- it with the row as it is now, so a changed import or an unchecked row is found
-- without anyone having to remember it, and an unchanged one is never checked again.

alter table public.events
  add column if not exists moderation_hash text,
  add column if not exists moderated_at timestamptz,
  add column if not exists moderation_blocked boolean not null default false,
  add column if not exists moderation_reasons text[],
  add column if not exists moderation_dropped_cover text;

alter table public.events
  add column if not exists moderation_stale boolean
  generated always as (
    moderation_hash is distinct from md5(
      coalesce(title, '') || chr(31) || coalesce(description, '') || chr(31) || coalesce(cover_url, '')
    )
  ) stored;

comment on column public.events.moderation_hash is
  'md5 do título, da descrição e da capa que a moderação verificou (#22 A).';
comment on column public.events.moderated_at is
  'Quando o conteúdo foi verificado pela moderação.';
comment on column public.events.moderation_blocked is
  'Verdadeiro quando a moderação recusou o conteúdo importado: o evento some da descoberta.';
comment on column public.events.moderation_reasons is
  'Categorias da moderação que recusaram o conteúdo (sexual, violence…).';
comment on column public.events.moderation_dropped_cover is
  'Capa da fonte que a moderação recusou ou não conseguiu ler: a próxima importação não a verifica de novo.';
comment on column public.events.moderation_stale is
  'Verdadeiro quando o conteúdo atual ainda não foi verificado pela moderação.';

-- The moderation pass reads only the stale rows.
create index if not exists events_moderation_stale_idx
  on public.events (id) where moderation_stale;

-- Discovery leaves out what the moderation refused. Same signature and body as in
-- 20261013000000_event_end_and_checkin_window, plus that one filter.
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
as $fn$
  with nearby as (
    select e.id,
           e.event_date,
           e.event_embedding,
           6371.0 * 2 * asin(least(1.0, sqrt(
               power(sin(radians(e.lat - user_lat) / 2), 2)
             + cos(radians(user_lat)) * cos(radians(e.lat))
               * power(sin(radians(e.long - user_lng) / 2), 2)
           ))) as distance_km
      from public.events e
     where (e.event_date is null or coalesce(e.ends_at, e.event_date) >= now())
       and not e.moderation_blocked
       and e.lat between user_lat - radius_km / 111.0 and user_lat + radius_km / 111.0
       and (kinds is null or e.kind = any (kinds))
       and (categories is null or e.category = any (categories))
       and (e.kind = 'place' or date_from is null or e.event_date >= date_from)
       and (e.kind = 'place' or date_to is null or e.event_date <= date_to)
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
     (query_embedding is not null and n.event_embedding is null),
     case when query_embedding is not null then n.event_embedding <=> query_embedding end,
     n.event_date asc nulls last,
     n.id
   limit 200;
$fn$;

-- Only the backend calls it (service_role). See the functions rule in CLAUDE.md.
revoke execute on function public.match_events(extensions.vector, double precision, double precision, double precision, text[], text[], timestamptz, timestamptz, numeric)
  from public, anon, authenticated;
grant execute on function public.match_events(extensions.vector, double precision, double precision, double precision, text[], text[], timestamptz, timestamptz, numeric)
  to service_role;
