-- "Pular a vez" (#5 part 1, decision 2026-10-09: only someone already called).
--
-- The creator sees someone called (`called`) who is not coming (they said they are
-- late, or left) and passes the turn now instead of waiting for `call_ttl_minutes`.
-- The person becomes `no_show`, like an expired call, so `rsvp_recall` still brings
-- them back if they turn up; with `auto_call_next` the next in line is called at once.
--
-- SECURITY DEFINER with the event row locked, like the other rsvp_* functions. The
-- creator comes from the validated JWT in the backend, never from a client: EXECUTE
-- is revoked from public, anon and authenticated (see 20260922000000).

create or replace function public.rsvp_skip(
  p_event_id uuid, p_rsvp_id uuid, p_creator_id uuid
)
returns public.event_rsvps
language plpgsql security definer set search_path = public as $$
declare
  v_creator uuid;
  v_auto    boolean;
  v_ttl     int;
  v_next    uuid;
  v_row     public.event_rsvps;
begin
  perform public.rsvp_sweep_expired_calls(p_event_id);

  select creator_id, auto_call_next, call_ttl_minutes into v_creator, v_auto, v_ttl
    from public.events where id = p_event_id for update;
  if not found then
    raise exception 'event_not_found' using errcode = 'P0002';
  end if;
  if v_creator is distinct from p_creator_id then
    raise exception 'not_event_creator' using errcode = 'P0003';
  end if;

  update public.event_rsvps
     set status            = 'no_show',
         no_show_count     = no_show_count + 1,
         waitlist_position = null,
         updated_at        = now()
   where id = p_rsvp_id and event_id = p_event_id
     and status = 'called'
  returning * into v_row;

  if not found then
    raise exception 'rsvp_not_skippable' using errcode = 'P0007';
  end if;

  if v_auto then
    select id into v_next
      from public.event_rsvps
     where event_id = p_event_id and status = 'waitlisted'
     order by waitlist_position
     limit 1;

    if v_next is not null then
      update public.event_rsvps
         set status            = 'called',
             called_at         = now(),
             call_expires_at   = now() + make_interval(mins => v_ttl),
             waitlist_position = null,
             updated_at        = now()
       where id = v_next;
    end if;
  end if;

  return v_row;
end $$;

revoke all on function public.rsvp_skip(uuid, uuid, uuid) from public;
revoke execute on function public.rsvp_skip(uuid, uuid, uuid) from anon, authenticated;
grant execute on function public.rsvp_skip(uuid, uuid, uuid) to service_role;
