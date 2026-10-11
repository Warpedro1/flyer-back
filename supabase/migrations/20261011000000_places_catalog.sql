-- Catálogo de Recife (Lote 5B): lugares, eventos ligados a lugares e filtros na descoberta.
--
-- Decisões (2026-10-11):
--   * "evento" passa a ser qualquer coisa para fazer. Lugares permanentes (restaurante,
--     academia, praia...) vivem na mesma tabela, com kind = 'place' e sem data;
--   * um evento pode acontecer num lugar (place_id). A ligação é feita pela importação
--     (local coincidente) ou por quem cria o evento na app;
--   * filtros da descoberta: tipo, categoria, data, distância e valor. Os lugares passam
--     sempre nos filtros de data e de valor (não têm data nem preço exato);
--   * price = 0 é grátis; price null é preço desconhecido (a fonte não o deu), e esses
--     eventos ficam de fora quando se filtra por valor.

alter table public.events
  add column if not exists kind text not null default 'event',
  add column if not exists place_id uuid references public.events (id) on delete set null,
  add column if not exists opening_hours jsonb,
  add column if not exists price_level smallint,
  add column if not exists source_url text,
  add column if not exists cover_url text;

alter table public.events
  add constraint events_kind_check check (kind in ('event', 'place')),
  -- Só eventos apontam para um lugar; um lugar não está dentro de outro.
  add constraint events_place_id_only_on_events check (place_id is null or kind = 'event'),
  add constraint events_price_level_check check (price_level is null or price_level between 1 and 4);

create index if not exists events_place_id_idx on public.events (place_id) where place_id is not null;
create index if not exists events_kind_idx on public.events (kind);

comment on column public.events.kind is
  'event: tem data (ou foi criado na app). place: lugar permanente, sem data.';
comment on column public.events.place_id is
  'Lugar (kind = place) onde o evento acontece.';
comment on column public.events.opening_hours is
  'Horários do lugar, como a fonte os dá: [{"day": "segunda-feira", "hours": "08:00–22:00"}].';
comment on column public.events.price_level is
  'Nível de preço do lugar, de 1 a 4 (barato a caro). Os eventos usam price.';
comment on column public.events.source_url is 'Página do item na fonte (Google Maps, Sympla).';
comment on column public.events.cover_url is 'Imagem pública da fonte, quando o evento não tem media própria.';

-- O índice único parcial não serve ao upsert: o PostgREST manda
-- ON CONFLICT (source, source_event_id) sem o predicado, e o Postgres só aceita um
-- índice parcial se o predicado vier no pedido (erro 42P10). Uma restrição normal faz
-- o mesmo trabalho: os NULL são distintos, por isso os eventos criados na app (sem
-- source) nunca colidem entre si.
drop index if exists public.events_source_source_event_id_key;
alter table public.events
  add constraint events_source_source_event_id_key unique (source, source_event_id);

-- Taxonomia nova (slugs estáveis; os rótulos vivem no frontend).
update public.events
   set category = case lower(trim(category))
                    when 'music' then 'musica_show'
                    when 'sport' then 'desporto'
                    when 'food' then 'comida_bebida'
                    when 'art' then 'arte_cultura'
                    when 'nightlife' then 'vida_noturna'
                    when 'outdoor' then 'praia_natureza'
                    else 'outros'
                  end
 where category is not null
   and category not in (
     'comida_bebida', 'vida_noturna', 'musica_show', 'arte_cultura', 'teatro_cinema',
     'palestra_conhecimento', 'cursos_oficinas', 'estudo_leitura', 'debate_comunidade',
     'voluntariado', 'desporto', 'academia_exercicio', 'bem_estar', 'praia_natureza',
     'turismo_passeio', 'jogos', 'compras_lojas', 'feira_mercado', 'familia_criancas',
     'religiao_espiritualidade', 'tecnologia_negocios', 'outros'
   );

-- A função ganha parâmetros: a assinatura muda, por isso a antiga sai primeiro (senão
-- ficavam as duas e o PostgREST não sabia qual chamar).
drop function if exists public.match_events(extensions.vector, double precision, double precision, double precision);

create function public.match_events(
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
     where (e.event_date is null or e.event_date >= now())
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
