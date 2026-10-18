-- Taste agent keywords (#31, #26 part 2, #22 part B; decisions 2026-10-18).
--
-- The agent reads what a person wrote in the onboarding, or what an event or place
-- says, and turns it into pt-BR keywords and a one-to-two-sentence summary. The
-- vectors are now made from those, so both are stored: a vector can be rebuilt
-- without calling the LLM again, a recommendation can be explained, and the person
-- will be able to correct their keywords (#24).
--
-- `events.keywords_hash` is the md5 of the title, description and category the
-- keywords were made from (`taste_agent.event_content_hash`, same expression).
-- `keywords_stale` compares it with the row as it is now, so an import that changed
-- a description, or an event without keywords, is found by the reprocessing
-- without the ETL having to remember it.

alter table public.profiles
  add column if not exists interest_keywords text[],
  add column if not exists interest_summary text;

comment on column public.profiles.interest_keywords is
  'Palavras-chave dos gostos da pessoa, da mais importante para a menos (agente de gostos, #31). Nulo = ainda não analisado.';
comment on column public.profiles.interest_summary is
  'Resumo dos gostos da pessoa em 1 a 2 frases (agente de gostos, #31).';

alter table public.events
  add column if not exists keywords text[],
  add column if not exists keywords_summary text,
  add column if not exists keywords_hash text;

alter table public.events
  add column if not exists keywords_stale boolean
  generated always as (
    keywords_hash is distinct from md5(
      coalesce(title, '') || chr(31) || coalesce(description, '') || chr(31) || coalesce(category, '')
    )
  ) stored;

comment on column public.events.keywords is
  'Palavras-chave do evento ou lugar, da mais importante para a menos (agente de gostos, #22 B).';
comment on column public.events.keywords_summary is
  'Resumo do evento ou lugar em 1 a 2 frases (agente de gostos, #22 B).';
comment on column public.events.keywords_hash is
  'md5 do título, da descrição e da categoria de que as palavras-chave saíram.';
comment on column public.events.keywords_stale is
  'Verdadeiro quando as palavras-chave faltam ou não correspondem mais ao texto atual.';

-- The reprocessing reads only the stale rows.
create index if not exists events_keywords_stale_idx
  on public.events (id) where keywords_stale;

alter table public.user_taste_documents
  add column if not exists analysis jsonb;

comment on column public.user_taste_documents.analysis is
  'Análise da resposta pelo agente de gostos (#31): obras e coisas citadas, palavras-chave e resumo. Nulo = não analisada.';
