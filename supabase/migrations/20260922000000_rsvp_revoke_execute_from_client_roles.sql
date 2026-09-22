-- CORREÇÃO DE SEGURANÇA sobre 20260921000000_event_capacity_waitlist.
-- Já aplicada ao projeto Supabase; fica aqui para o repo ser a fonte da verdade.
--
-- O que estava mal
-- ----------------
-- As funções rsvp_* ficaram executáveis pelos roles `anon` e `authenticated`,
-- logo chamáveis diretamente em POST /rest/v1/rpc/<funcao> com a chave
-- publicável — que está no bundle do frontend, por desenho.
--
-- O `revoke all ... from public` da migração original não bastou: o Supabase
-- concede EXECUTE a `anon` e `authenticated` por ALTER DEFAULT PRIVILEGES no
-- momento em que a função é criada, e essa concessão é separada da do role
-- PUBLIC. Revogar de PUBLIC não toca nas duas.
--
-- (A migração 20260511000000_onboarding_profiles tem o mesmo padrão ineficaz em
-- increment_interest_sync_count, e continua exposta. Fica sinalizado.)
--
-- Porque é grave
-- --------------
-- Estas funções são SECURITY DEFINER — ignoram RLS — e recebem a identidade como
-- PARÂMETRO. Quem chama escolhe o parâmetro, portanto qualquer pessoa que abra a
-- app conseguia:
--   * rsvp_admit      admitir quem quisesse sem QR nenhum. O portão inteiro
--                     ficava contornável: nem token, nem assinatura, nem jti.
--   * rsvp_cancel     cancelar a inscrição de terceiros.
--   * rsvp_join       inscrever terceiros, ou encher a lotação de um evento alheio.
--   * rsvp_call_next  a validação `creator_id = p_creator_id` não vale nada
--     rsvp_recall     quando é o atacante que passa o p_creator_id.
--
-- A identidade tem de vir do JWT. É o backend que a extrai (app/api/deps.py) e só
-- depois chama estas funções com a service_role; nenhum cliente as deve alcançar.

revoke execute on function public.rsvp_join(uuid, uuid)                   from anon, authenticated;
revoke execute on function public.rsvp_cancel(uuid, uuid)                 from anon, authenticated;
revoke execute on function public.rsvp_call_next(uuid, uuid)              from anon, authenticated;
revoke execute on function public.rsvp_recall(uuid, uuid, uuid)           from anon, authenticated;
revoke execute on function public.rsvp_admit(uuid, uuid, text)            from anon, authenticated;
revoke execute on function public.rsvp_sweep_expired_calls(uuid)          from anon, authenticated;
revoke execute on function public.rsvp_taken_count(uuid)                  from anon, authenticated;

-- Função de trigger: corre como dona da tabela, não precisa de ser chamável.
revoke execute on function public.rsvp_sync_attendee_count()              from public, anon, authenticated;

-- O service_role (o backend) mantém o acesso.
grant execute on function public.rsvp_join(uuid, uuid)                    to service_role;
grant execute on function public.rsvp_cancel(uuid, uuid)                  to service_role;
grant execute on function public.rsvp_call_next(uuid, uuid)               to service_role;
grant execute on function public.rsvp_recall(uuid, uuid, uuid)            to service_role;
grant execute on function public.rsvp_admit(uuid, uuid, text)             to service_role;
grant execute on function public.rsvp_sweep_expired_calls(uuid)           to service_role;
grant execute on function public.rsvp_taken_count(uuid)                   to service_role;

-- Verificar depois de aplicar (deve devolver só service_role em cada linha):
--   select p.proname, string_agg(distinct g.grantee, ', ')
--     from pg_proc p join pg_namespace n on n.oid=p.pronamespace
--     left join information_schema.role_routine_grants g
--            on g.specific_schema='public' and g.routine_name=p.proname
--           and g.privilege_type='EXECUTE'
--    where n.nspname='public' and p.proname like 'rsvp%' group by p.proname;
