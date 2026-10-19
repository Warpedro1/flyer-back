-- What pleases most people first (decision 2026-10-19).
--
-- The Google Maps import keeps only popular places (enough reviews and a good
-- rating) and stores how many reviews each has. Discovery without a profile (no
-- query vector) then shows the events of broad appeal before the others, each group
-- by date, and the places most reviewed first. With a profile the order stays by
-- similarity, as before.

-- `rating` was created in the dashboard, before the versioned migrations; it is
-- declared here so the order below also holds on a fresh database.
alter table public.events
  add column if not exists rating numeric(3,2),
  add column if not exists reviews_count integer check (reviews_count >= 0);

comment on column public.events.reviews_count is
  'Quantas avaliações o lugar tem no Google (a popularidade usada na importação e na descoberta sem perfil).';

-- Same signature, filters and body as in 20261018030000_event_moderation; only the
-- order without a query vector changes. The categories of the events that go first
-- (shows, parties, festivals and theatre) are `taxonomy.BROAD_APPEAL_CATEGORIES` (a
-- test keeps the two lists the same).
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
           e.kind,
           e.category,
           e.event_date,
           e.reviews_count,
           e.rating,
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
     -- Without a profile: shows, parties, festivals and theatre first (places sort
     -- after every event).
     case when query_embedding is null and n.kind = 'event' then not coalesce(n.category = any (array[
       'musica_show', 'vida_noturna', 'teatro_cinema'
     ]::text[]), false) end,
     n.event_date asc nulls last,
     -- Without a profile: the most reviewed places first, then the better rated.
     case when query_embedding is null then n.reviews_count end desc nulls last,
     case when query_embedding is null then n.rating end desc nulls last,
     n.id
   limit 200;
$fn$;

-- Only the backend calls it (service_role). See the functions rule in CLAUDE.md.
revoke execute on function public.match_events(extensions.vector, double precision, double precision, double precision, text[], text[], timestamptz, timestamptz, numeric)
  from public, anon, authenticated;
grant execute on function public.match_events(extensions.vector, double precision, double precision, double precision, text[], text[], timestamptz, timestamptz, numeric)
  to service_role;
