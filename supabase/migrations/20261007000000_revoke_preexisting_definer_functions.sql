-- CORREÇÃO DE SEGURANÇA (issue #6): funções SECURITY DEFINER anteriores às rsvp_*
-- continuavam executáveis pelos roles `anon` e `authenticated`.
--
-- Mesma classe de falha corrigida em 20260922000000_rsvp_revoke_execute_from_client_roles:
-- o Supabase concede EXECUTE a `anon` e `authenticated` por ALTER DEFAULT PRIVILEGES
-- quando a função é criada, e essa concessão é separada da do role PUBLIC. O
-- `REVOKE ALL ... FROM PUBLIC` das migrações originais não lhes tocou.
--
-- O que um cliente conseguia, com a chave publicável do bundle do frontend:
--   * increment_interest_sync_count  inflacionar o contador de qualquer pessoa,
--                                    tirando-a mais cedo dos pesos de âncora.
--   * match_user_taste_documents     LER os documentos de gosto de qualquer pessoa
--                                    (recebe o p_user_id como parâmetro).
--   * handle_new_user                função de trigger; não devia ser chamável.
--
-- O backend chama as duas primeiras com a service_role, que mantém o acesso.
-- O trigger de auth continua a disparar: o PostgreSQL só verifica EXECUTE na
-- função de trigger quando o trigger é criado, não quando dispara.

revoke execute on function public.increment_interest_sync_count(uuid)                            from public, anon, authenticated;
revoke execute on function public.match_user_taste_documents(uuid, extensions.vector, integer)   from public, anon, authenticated;
revoke execute on function public.handle_new_user()                                              from public, anon, authenticated;

grant execute on function public.increment_interest_sync_count(uuid)                             to service_role;
grant execute on function public.match_user_taste_documents(uuid, extensions.vector, integer)    to service_role;

-- handle_new_user é SECURITY DEFINER sem search_path fixo (advisor
-- function_search_path_mutable): quem controlasse o search_path podia trocar as
-- tabelas que ela resolve. O corpo já qualifica tudo com public., por isso um
-- search_path vazio não muda o comportamento.
alter function public.handle_new_user() set search_path = '';

-- Verificar depois de aplicar (anon_exec e auth_exec devem ser false nas 3):
--   select p.proname,
--          has_function_privilege('anon', p.oid, 'execute')          as anon_exec,
--          has_function_privilege('authenticated', p.oid, 'execute') as auth_exec
--     from pg_proc p join pg_namespace n on n.oid = p.pronamespace
--    where n.nspname = 'public'
--      and p.proname in ('increment_interest_sync_count', 'match_user_taste_documents', 'handle_new_user');
