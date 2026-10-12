-- Troféus (2026-10-12): todo o evento dá troféu no check-in. Sem troféu personalizado,
-- o backend cria um "Presença: <título>" no primeiro check-in. Um evento tem um único
-- modelo de troféu: a restrição torna essa criação segura quando duas pessoas fazem
-- check-in ao mesmo tempo (o segundo insert é ignorado e o id é lido de volta).
-- Num evento recorrente cada ocorrência é um evento, por isso tem o seu troféu.

alter table public.trophy_templates
  add constraint trophy_templates_event_id_key unique (event_id);

comment on column public.trophy_templates.icon_url is
  'Caminho da imagem no bucket event-media ({user_id}/...), assinado pelo backend ao ler. Null = ícone do Flyer.';
