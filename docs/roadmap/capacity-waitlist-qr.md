# Próximo passo — Lotação, lista de espera e check-in por QR

> Estado: **especificação**, ainda não implementado.
> Escopo: backend (`FlyerBack`). O lado do cliente está em
> `Flyer/docs/roadmap/capacity-waitlist-qr.md` e consome os contratos daqui.

## Objetivo

Um evento passa a poder ter **lotação máxima**. Um utilizador confirma presença
("vou"). Quando a lotação está cheia, a confirmação entra numa **fila de lista de
espera** por ordem de chegada. O criador **chama o próximo da vez**; a prova de que
a pessoa é mesmo a da vez e está na lista é um **QR code** que o participante
mostra e o criador lê.

Dois requisitos explícitos do produto:

1. **Eventos sem limite de pessoas continuam a existir.** `capacity IS NULL` =
   sem limite: toda a gente entra direto como `confirmed` e a fila nunca é usada.
   É o comportamento por omissão — a lotação é opt-in na criação do evento.
2. **A chamada tem prazo e a fila não pode parar.** Quem é chamado tem
   `call_ttl_minutes` para aparecer e ser lido. Se não aparecer, a chamada expira,
   a pessoa sai da frente e **o próximo é chamado automaticamente**. Uma pessoa
   que não aparece nunca bloqueia a fila.

## Máquina de estados do RSVP

```
            POST /rsvp                        criador: call-next
 (nada) ──────────────┬──► confirmed ────────────────────────────────┐
                      │                                              │
                      └──► waitlisted ──► called ───────────────────┤
                                 ▲          │ passou call_expires_at │ QR lido
                                 │          ▼                        ▼
                                 │       no_show                 admitted
                                 └── criador: "rechamar"
 qualquer estado ── DELETE /rsvp ──► cancelled (liberta vaga → promove o próximo)
```

| Estado | Significado | Ocupa vaga? |
| --- | --- | --- |
| `confirmed` | Vaga garantida; ainda não entrou. | sim |
| `waitlisted` | Na fila, com `waitlist_position`. | não |
| `called` | Chamado pelo criador; a contar `call_expires_at`. | sim |
| `admitted` | QR validado na porta. Presença efetiva. | sim |
| `no_show` | Foi chamado e o prazo passou sem leitura. | não |
| `cancelled` | Desistiu ou foi removido. | não |

Regras:

- Ocupação = `confirmed` + `called` + `admitted`.
- `capacity IS NULL` → sem limite; `rsvp_join` devolve sempre `confirmed`.
- Quem ocupava vaga e cancela liberta-a, e o primeiro `waitlisted` é promovido a
  `confirmed` **na mesma transação**.
- Eventos recorrentes já geram **uma linha por ocorrência**
  (`app/services/event_recurrence.py`), por isso a lotação é naturalmente por
  ocorrência — não é preciso tratamento especial.

### O prazo da chamada e a varredura preguiçosa

Não há scheduler neste backend (sem Celery, sem cron) e não se vai introduzir um
só para isto. A expiração é **preguiçosa**: toda a operação que toca na fila
começa por chamar `rsvp_sweep_expired_calls(p_event_id)`, que numa única
transação:

1. marca `no_show` todos os `called` com `call_expires_at < now()`;
2. se o evento tem `auto_call_next = true` (por omissão), chama imediatamente o
   próximo `waitlisted` para cada vaga assim libertada.

A varredura corre em `POST /rsvp`, `DELETE /rsvp`, `GET /events/{id}/rsvp`,
`GET /events/{id}/attendees`, `POST /waitlist/call-next` e
`POST /attendance/scan`. Como o ecrã do criador faz polling de 15 s em
`/attendees`, na prática a fila anda sozinha enquanto alguém estiver a olhar para
ela. O caso "ninguém abriu o ecrã durante uma hora" resolve-se na primeira
operação seguinte, que expira tudo o que estava vencido de uma vez — o estado
lido é sempre o correto, independentemente de quando foi lido.

`no_show` é terminal por si só, mas o criador pode "rechamar" alguém (volta a
`called` com prazo novo), para o caso de a pessoa chegar atrasada.

## Migração SQL

Ficheiro a criar: `supabase/migrations/<timestamp>_event_capacity_waitlist.sql`.
(O `CLAUDE.md` aponta `migrations/` para SQL, mas as quatro migrações mais
recentes vivem em `supabase/migrations/` com prefixo de timestamp — seguir a
convenção recente e alinhar o `CLAUDE.md` depois.)

```sql
-- 1. Lotação no evento
alter table public.events
  add column if not exists capacity integer
    check (capacity is null or capacity > 0),
  add column if not exists waitlist_enabled boolean not null default true,
  add column if not exists call_ttl_minutes integer not null default 10
    check (call_ttl_minutes between 1 and 240),
  add column if not exists auto_call_next boolean not null default true;

comment on column public.events.capacity is
  'Lotação máxima. NULL = evento sem limite de pessoas (a fila nunca é usada).';
comment on column public.events.call_ttl_minutes is
  'Minutos que a pessoa chamada tem para aparecer antes de virar no_show.';
comment on column public.events.auto_call_next is
  'Quando uma chamada expira ou é libertada, chama o próximo da fila sozinho.';

-- 2. Estados possíveis
do $$ begin
  create type public.rsvp_status as enum
    ('confirmed','waitlisted','called','admitted','no_show','cancelled');
exception when duplicate_object then null; end $$;

-- 3. Inscrições
create table if not exists public.event_rsvps (
  id                uuid primary key default gen_random_uuid(),
  event_id          uuid not null references public.events(id) on delete cascade,
  user_id           uuid not null references public.profiles(id) on delete cascade,
  status            public.rsvp_status not null default 'confirmed',
  waitlist_position integer,
  called_at         timestamptz,
  call_expires_at   timestamptz,
  admitted_at       timestamptz,
  no_show_count     integer not null default 0,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  unique (event_id, user_id)
);

create index if not exists event_rsvps_queue_idx
  on public.event_rsvps (event_id, waitlist_position)
  where status = 'waitlisted';
-- Suporta a varredura de chamadas vencidas sem sequential scan.
create index if not exists event_rsvps_expiring_idx
  on public.event_rsvps (call_expires_at)
  where status = 'called';
create index if not exists event_rsvps_user_idx
  on public.event_rsvps (user_id);

alter table public.event_rsvps enable row level security;

-- O participante lê a própria inscrição...
create policy "rsvp_select_own" on public.event_rsvps
  for select to authenticated using (user_id = auth.uid());

-- ...e o criador do evento vê a lista toda do seu evento.
create policy "rsvp_select_as_creator" on public.event_rsvps
  for select to authenticated using (
    exists (select 1 from public.events e
            where e.id = event_id and e.creator_id = auth.uid())
  );

-- Sem policies de INSERT/UPDATE: todas as escritas passam pelas RPCs
-- SECURITY DEFINER abaixo, que é onde a concorrência é resolvida.
```

### Por que RPCs e não escrita direta pelo PostgREST

O backend fala com o Supabase por PostgREST (`app/services/db_service.py`), que
não dá transações multi-statement. Duas pessoas a confirmar a última vaga ao
mesmo tempo dariam 2 × `confirmed` acima da lotação. As operações críticas ficam
em funções Postgres `SECURITY DEFINER` com `select ... for update` no evento, no
mesmo padrão já usado por `increment_interest_sync_count` e `match_events`.

```sql
-- Varredura: expira chamadas vencidas e faz a fila andar. Idempotente.
create or replace function public.rsvp_sweep_expired_calls(p_event_id uuid)
returns integer
language plpgsql security definer set search_path = public as $$
declare
  v_auto  bool;
  v_ttl   int;
  v_freed int;
  v_next  public.event_rsvps;
begin
  select auto_call_next, call_ttl_minutes into v_auto, v_ttl
  from public.events where id = p_event_id for update;
  if not found then
    return 0;
  end if;

  with expired as (
    update public.event_rsvps
       set status = 'no_show',
           no_show_count = no_show_count + 1,
           waitlist_position = null,
           updated_at = now()
     where event_id = p_event_id
       and status = 'called'
       and call_expires_at < now()
    returning 1
  )
  select count(*) into v_freed from expired;

  if v_freed = 0 or not v_auto then
    return v_freed;
  end if;

  -- Uma vaga libertada = uma chamada nova, para a fila não parar.
  for i in 1..v_freed loop
    update public.event_rsvps
       set status = 'called',
           called_at = now(),
           call_expires_at = now() + make_interval(mins => v_ttl),
           waitlist_position = null,
           updated_at = now()
     where id = (
       select id from public.event_rsvps
        where event_id = p_event_id and status = 'waitlisted'
        order by waitlist_position
        limit 1
     )
    returning * into v_next;
    exit when v_next.id is null;   -- fila vazia
  end loop;

  return v_freed;
end $$;


-- rsvp_join: confirma ou põe em fila, atomicamente.
create or replace function public.rsvp_join(p_event_id uuid, p_user_id uuid)
returns public.event_rsvps
language plpgsql security definer set search_path = public as $$
declare
  v_capacity int;
  v_waitlist bool;
  v_taken    int;
  v_row      public.event_rsvps;
begin
  perform public.rsvp_sweep_expired_calls(p_event_id);

  -- Trava a linha do evento: serializa quem disputa a última vaga.
  select capacity, waitlist_enabled into v_capacity, v_waitlist
  from public.events where id = p_event_id for update;
  if not found then
    raise exception 'event_not_found' using errcode = 'P0002';
  end if;

  -- Reentrante: quem já está inscrito recebe a própria linha de volta.
  select * into v_row from public.event_rsvps
  where event_id = p_event_id and user_id = p_user_id;
  if found and v_row.status not in ('cancelled','no_show') then
    return v_row;
  end if;

  -- capacity IS NULL = evento sem limite: entra sempre.
  select count(*) into v_taken from public.event_rsvps
  where event_id = p_event_id and status in ('confirmed','called','admitted');

  if v_capacity is null or v_taken < v_capacity then
    insert into public.event_rsvps (event_id, user_id, status)
    values (p_event_id, p_user_id, 'confirmed')
    on conflict (event_id, user_id) do update
      set status = 'confirmed', waitlist_position = null, updated_at = now()
    returning * into v_row;
  elsif v_waitlist then
    insert into public.event_rsvps (event_id, user_id, status, waitlist_position)
    values (p_event_id, p_user_id, 'waitlisted',
            coalesce((select max(waitlist_position) from public.event_rsvps
                      where event_id = p_event_id), 0) + 1)
    on conflict (event_id, user_id) do update
      set status = 'waitlisted', updated_at = now(),
          waitlist_position = coalesce(
            (select max(waitlist_position) from public.event_rsvps
              where event_id = p_event_id), 0) + 1
    returning * into v_row;
  else
    raise exception 'event_full' using errcode = 'P0001';
  end if;

  return v_row;
end $$;
```

`rsvp_call_next(p_event_id, p_creator_id)` e `rsvp_cancel(p_event_id, p_user_id)`
seguem a mesma forma:

- **`rsvp_call_next`** — valida `creator_id = p_creator_id`, corre a varredura,
  tranca o evento, tira o menor `waitlist_position` e marca `called` com
  `called_at = now()` e `call_expires_at = now() + call_ttl_minutes`. Devolve
  `NULL` se a fila está vazia.
- **`rsvp_cancel`** — marca `cancelled` e, se a linha ocupava vaga, promove o
  primeiro `waitlisted` a `confirmed` na mesma transação.
- **`rsvp_recall(p_event_id, p_rsvp_id, p_creator_id)`** — devolve um `no_show` a
  `called` com prazo novo (a pessoa chegou atrasada).

Manter `events.attendee_count` (coluna que já existe e já é servida por
`EventRead`) sincronizado com a ocupação através de um trigger `after insert or
update of status` em `event_rsvps`, para não obrigar a um `count` por leitura.

## O QR code

**Requisito de segurança:** o QR não pode ser um identificador estático. Uma foto
do ecrã de alguém seria suficiente para entrar no lugar dessa pessoa.

O QR transporta um **token HMAC de vida curta**, emitido pelo backend e renovado
pelo telemóvel do participante a cada ~30 s:

```
payload = { rsvp, evt, sub, jti, iat, exp }        # exp = iat + 45s
token   = JWT HS256 assinado com RSVP_QR_SECRET
```

- Assinado com um segredo novo `RSVP_QR_SECRET` (`app/core/config.py`), **não**
  com `SUPABASE_JWT_SECRET` — chaves separadas por função.
- `PyJWT` já é dependência do projeto; usar `jwt.encode` / `jwt.decode` com
  `HS256` evita escrever criptografia à mão.
- `jti` torna o token de **uso único**: a leitura grava o `jti` em
  `rsvp_scan_nonces (jti text primary key, scanned_at timestamptz default now())`
  com `insert` — a violação de unicidade é a deteção de replay. Limpar por
  `scanned_at < now() - interval '1 day'`.
- A validação na porta confere, por esta ordem: assinatura → `exp` → `evt`
  corresponde ao evento que está a ser lido → o RSVP existe → o estado é
  `confirmed` ou `called` → `jti` ainda não usado. Só então grava `admitted`.
- Um RSVP em `waitlisted` ou `no_show` gera token na mesma (o ecrã do
  participante mostra sempre o QR), mas a leitura devolve `409` com a razão. É
  assim de propósito: o porteiro percebe *porquê* que a pessoa não entra em vez
  de ver um erro genérico.

## Endpoints

Rotas novas em `app/api/rsvp.py` (ficheiro novo, registado no `main.py`;
`events.py` já tem 300 linhas e o domínio é distinto). Com `@limiter.limit(...)`
nas mutações, como o resto do projeto.

### Participante

| Método | Rota | Resposta |
| --- | --- | --- |
| `POST` | `/events/{event_id}/rsvp` | `RsvpRead` — `confirmed` ou `waitlisted` + posição |
| `DELETE` | `/events/{event_id}/rsvp` | `204`; liberta vaga e promove o próximo |
| `GET` | `/events/{event_id}/rsvp` | `RsvpRead` do próprio, `404` se não inscrito |
| `GET` | `/events/{event_id}/rsvp/qr` | `{ token, expires_at }` — renovar a cada 30 s |

### Criador do evento

| Método | Rota | Resposta |
| --- | --- | --- |
| `GET` | `/events/{event_id}/attendees` | `{ capacity, taken, confirmed[], waitlist[], called[] }` |
| `POST` | `/events/{event_id}/waitlist/call-next` | `RsvpRead` do chamado, ou `204` se a fila estiver vazia |
| `POST` | `/events/{event_id}/waitlist/{rsvp_id}/recall` | `RsvpRead` — devolve um `no_show` a `called` |
| `POST` | `/events/{event_id}/attendance/scan` | `{ ok, status, attendee: ProfileBrief }` |

Todas as rotas de criador começam por confirmar `events.creator_id == user_id` e
devolvem `403` caso contrário.

### Schemas (`app/models/schemas.py`)

```python
class RsvpStatus(str, Enum):           # espelha o enum Postgres
    confirmed = "confirmed"
    waitlisted = "waitlisted"
    called = "called"
    admitted = "admitted"
    no_show = "no_show"
    cancelled = "cancelled"


class RsvpRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    event_id: str
    user_id: str
    status: RsvpStatus
    waitlist_position: int | None = None
    ahead_count: int | None = None      # quantos estão à frente na fila
    called_at: datetime | None = None
    call_expires_at: datetime | None = None
    admitted_at: datetime | None = None
    created_at: datetime | None = None


class EventCapacityRead(BaseModel):     # estado da lotação, embutido em EventRead
    model_config = ConfigDict(extra="forbid")
    capacity: int | None = None         # None = evento sem limite
    taken: int = 0
    waitlist_count: int = 0
    my_status: RsvpStatus | None = None
    my_waitlist_position: int | None = None
```

`EventCreate` ganha:

```python
capacity: int | None = Field(default=None, gt=0)       # None = sem limite
waitlist_enabled: bool = True
call_ttl_minutes: int = Field(default=10, ge=1, le=240)
auto_call_next: bool = True
```

`EventRead` ganha `capacity`, `call_ttl_minutes` e
`capacity_state: EventCapacityRead | None`. Como ambos usam
`ConfigDict(extra="forbid")`, `types/index.ts` no frontend tem de ser atualizado
no mesmo passo.

### Códigos de erro

| Situação | HTTP | `detail` |
| --- | --- | --- |
| Evento cheio e `waitlist_enabled = false` | `409` | `Evento esgotado.` |
| Já inscrito | `200` | devolve a inscrição existente (idempotente) |
| Não é o criador | `403` | `Só o criador do evento pode fazer isto.` |
| QR expirado | `401` | `QR expirado. Pede um novo ao participante.` |
| QR já usado | `409` | `Este QR já foi lido.` |
| QR de outro evento | `403` | `Este QR não é deste evento.` |
| Pessoa ainda na fila | `409` | `Ainda não é a vez desta pessoa.` |
| Chamada expirada (`no_show`) | `409` | `A chamada desta pessoa expirou.` |

## Tempo real (opcional)

O `pusher` está no `requirements.txt` e no `Settings`, mas **não é usado em lado
nenhum do código hoje**. Esta é a primeira funcionalidade que o justifica: quando
`call_next` (ou a varredura) promove alguém, publicar `waitlist:called` no canal
`private-user-{user_id}` para o telemóvel acender sozinho, e `waitlist:expired`
quando o prazo passa. Fica atrás de um `if settings.PUSHER_APP_ID:` — sem
credenciais, a UI cai em polling de 15 s, que é também o que faz a varredura
preguiçosa andar.

## Testes a escrever (`tests/`)

- `test_rsvp_capacity.py` — confirma até encher; o seguinte entra na fila com
  posição 1; **com `capacity = None` nunca há fila**.
- `test_rsvp_cancel_promotes.py` — cancelar um `confirmed` promove o primeiro da fila.
- `test_rsvp_call_next.py` — chama por ordem; fila vazia devolve `204`;
  não-criador leva `403`.
- `test_rsvp_expiry.py` — `called` vencido vira `no_show`, o seguinte é chamado
  automaticamente, e a posição na fila de quem ficou para trás desce em 1;
  com `auto_call_next = false` só expira, sem chamar ninguém; `recall` devolve um
  `no_show` a `called` com prazo novo.
- `test_rsvp_qr.py` — token válido admite; token expirado `401`; segundo uso do
  mesmo `jti` `409`; token de outro evento `403`; assinatura adulterada `401`;
  token de quem está `waitlisted` dá `409` com a razão certa.

As RPCs de concorrência não são cobertas por estes testes (mockam o PostgREST,
como manda o `conftest.py` atual) — a garantia de que duas confirmações
simultâneas não ultrapassam a lotação precisa de um teste de integração contra
Postgres real, ou de verificação manual no Supabase.

## Ordem de implementação sugerida

1. Migração SQL + as RPCs (`rsvp_join`, `rsvp_cancel`, `rsvp_call_next`,
   `rsvp_sweep_expired_calls`, `rsvp_recall`), aplicadas no Supabase.
2. `schemas.py`: `RsvpStatus`, `RsvpRead`, `EventCapacityRead`, campos novos em
   `EventCreate` / `EventRead`.
3. `db_service.py`: um método por RPC + `get_attendees`, `get_rsvp`,
   `record_scan_nonce`, `mark_admitted`.
4. `app/api/rsvp.py` com as rotas do participante + testes.
5. `RSVP_QR_SECRET`, emissão e validação do token, `POST /attendance/scan` + testes.
6. Rotas do criador (`/attendees`, `/waitlist/call-next`, `/recall`) + testes.
7. Trigger de `attendee_count`.
8. Pusher (opcional).
