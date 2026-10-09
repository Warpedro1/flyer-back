-- CORREÇÃO DE SEGURANÇA: os clientes deixam de ler e escrever tabelas do schema public.
--
-- O Supabase concede, por ALTER DEFAULT PRIVILEGES, todos os privilégios nas tabelas
-- novas aos roles `anon` e `authenticated`. A RLS filtrava as linhas, mas as políticas
-- de leitura de `profiles`, `events`, `follows` e `trophies` são abertas ao role
-- `public`. Com a chave publicável que vai no bundle do frontend, qualquer pessoa na
-- internet conseguia, sem login:
--   * profiles   ler o email e o interest_embedding de todos os perfis não privados;
--   * follows    ler o grafo social inteiro;
--   * events, trophies, trophy_templates, event_media   ler tudo o que é público.
-- E `get_social_avg_embedding` (SECURITY INVOKER, sem search_path fixo) era chamável
-- com qualquer uuid em /rest/v1/rpc.
--
-- Desde o #7 (opção B) a API fala com o Supabase só com a service_role e o frontend
-- não lê nenhuma tabela diretamente: usa só o Auth e o Storage. Nada do que a app
-- faz depende destes privilégios. Não toca em:
--   * service_role — tem grants próprios em todas as tabelas;
--   * Storage — as políticas de storage.objects só usam auth.uid() e storage.foldername;
--   * handle_new_user — é SECURITY DEFINER, continua a criar o perfil no registo.
-- As políticas de RLS ficam como estão: deixam de ser alcançáveis pelos clientes, mas
-- continuam a ser a segunda linha se algum dia se voltar a conceder acesso.

revoke all on all tables    in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;

-- Tabelas, sequências e funções criadas daqui para a frente (pelo role postgres, que é
-- quem corre as migrações) também não nascem abertas aos clientes. A service_role
-- continua nos defaults, por isso o backend não precisa de grants extra.
alter default privileges for role postgres in schema public revoke all     on tables    from anon, authenticated;
alter default privileges for role postgres in schema public revoke all     on sequences from anon, authenticated;
alter default privileges for role postgres in schema public revoke execute on functions from public, anon, authenticated;

-- get_social_avg_embedding: só o backend a chama (descoberta com modo social).
alter function public.get_social_avg_embedding(uuid) set search_path = public, extensions;
revoke execute on function public.get_social_avg_embedding(uuid) from public, anon, authenticated;
grant  execute on function public.get_social_avg_embedding(uuid) to service_role;

-- Verificar depois de aplicar (as colunas anon_* e auth_* devem ser todas false):
--   select c.relname,
--          has_table_privilege('anon', c.oid, 'select')          as anon_select,
--          has_table_privilege('authenticated', c.oid, 'select') as auth_select,
--          has_table_privilege('service_role', c.oid, 'select')  as service_select
--     from pg_class c join pg_namespace n on n.oid = c.relnamespace
--    where n.nspname = 'public' and c.relkind = 'r';
