-- Descoberta de eventos (issue #4): a função match_events nunca foi versionada e não
-- existe em produção; o backend chama-a em POST /events/discover e recebia 404.
--
-- A coluna dos vetores já existe com outro nome: events.event_embedding vector(1536).
-- É essa que o ETL preenche, por isso fica e o backend passa a usá-la também (antes
-- escrevia em "embedding", que não existe, e a falha era engolida).
--
-- Decisões (2026-10-09):
--   * distância de cosseno (<=>): é indiferente à norma dos vetores;
--   * no máximo 200 resultados por pesquisa (o backend pagina por cima da lista);
--   * eventos sem vetor (falha da OpenAI ao criá-los) aparecem no fim, por data,
--     em vez de ficarem invisíveis;
--   * sem vetor de interesses (query_embedding null), devolve os eventos próximos
--     por data: é o fallback para quem ainda não fez o onboarding.
--   * eventos passados ficam de fora; os sem data ficam.

create index if not exists events_event_embedding_hnsw
  on public.events using hnsw (event_embedding extensions.vector_cosine_ops);

create or replace function public.match_events(
  query_embedding extensions.vector(1536),
  user_lat double precision,
  user_lng double precision,
  radius_km double precision
)
returns table (id uuid, similarity real, distance_km double precision)
language sql
stable
set search_path = public, extensions
as $$
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
     where (e.event_date is null or e.event_date >= now())
       -- Pré-filtro barato por latitude (1 grau ~ 111 km) antes da distância exata.
       and e.lat between user_lat - radius_km / 111.0 and user_lat + radius_km / 111.0
  )
  select n.id,
         case when query_embedding is not null and n.event_embedding is not null
              then (1 - (n.event_embedding <=> query_embedding))::real
         end as similarity,
         n.distance_km
    from nearby n
   where n.distance_km <= radius_km
   order by
     -- Com vetor de pesquisa: primeiro os eventos com vetor, por semelhança.
     (query_embedding is not null and n.event_embedding is null),
     case when query_embedding is not null then n.event_embedding <=> query_embedding end,
     -- Empate, eventos sem vetor, ou sem vetor de pesquisa: o mais próximo no tempo.
     n.event_date asc nulls last,
     n.id
   limit 200;
$$;

-- Só o backend a chama (service_role). Ver a regra das funções no CLAUDE.md.
revoke execute on function public.match_events(extensions.vector, double precision, double precision, double precision)
  from public, anon, authenticated;
grant execute on function public.match_events(extensions.vector, double precision, double precision, double precision)
  to service_role;
