"""Event discovery endpoints."""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.api.authz import (
    require_event_creator,
    require_media_paths_owned,
    require_place,
    require_trophy_icon,
)
from app.api.deps import DbServiceDep, get_current_user_with_token
from app.core.limiter import limiter
from app.core.tz import local_iso, parse_aware
from app.models.schemas import (
    EMBEDDING_DIMENSIONS,
    DiscoverRequest,
    EventCapacityRead,
    EventCreate,
    EventDescribeRead,
    EventDescribeRequest,
    EventEditRead,
    EventMediaEdit,
    EventMediaRead,
    EventRead,
    EventUpdate,
    MyEventsRole,
    MyEventsWhen,
    MyRsvpRead,
    RsvpStatus,
    TrophyEditRead,
    _EventFields,
)
from app.services.ai_service import ai_service
from app.services.checkin_service import checkin_method
from app.services.checkin_window import checkin_state
from app.services.event_describe import describe_event
from app.services.event_keywords import embed_app_events
from app.services.moderation import check_event_content, event_moderation_hash
from app.services.event_recurrence import expand_recurrence
from app.services.interest_sync import parse_interest_embedding
from app.services.onboarding_pipeline import ensure_interest_embedding
from app.services.trophies import default_trophy_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])

# Read by the frontend; listed in the CORS expose_headers in main.py.
DISCOVERY_MODE_HEADER = "X-Discovery-Mode"


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def filter_future_events(rows_in_order: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop events that are over; keep undated events and preserve order.

    An event is over at its end (`ends_at`), not its start: check-in opens at the start,
    so an event in progress must stay findable. Rows without an end use the start.
    Undated events (`event_date IS NULL`) stay visible on purpose. Naive datetimes are
    treated as UTC so the comparison is consistent regardless of how Postgres serializes them.
    """
    now = datetime.now(timezone.utc)
    kept: list[dict[str, Any]] = []
    for row in rows_in_order:
        dt = _parse_dt(row.get("ends_at") or row.get("event_date"))
        if dt is None:
            kept.append(row)
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= now:
            kept.append(row)
    return kept


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _row_to_event_read(
    row: dict[str, Any],
    media: list[EventMediaRead],
    capacity_state: EventCapacityRead | None = None,
) -> EventRead:
    checkin = checkin_state(row, datetime.now(timezone.utc))
    return EventRead(
        id=str(row["id"]),
        kind=row.get("kind") or "event",
        place_id=str(row["place_id"]) if row.get("place_id") else None,
        creator_id=str(row["creator_id"]) if row.get("creator_id") is not None else None,
        title=str(row["title"]),
        description=row.get("description"),
        category=row.get("category"),
        location_name=row.get("location_name"),
        lat=float(row["lat"]),
        long=float(row["long"]),
        event_date=_parse_dt(row.get("event_date")),
        ends_at=_parse_dt(row.get("ends_at")),
        checkin_opens_at=checkin.opens_at,
        checkin_closes_at=checkin.closes_at,
        checkin_status=checkin.status,
        checkin_method=checkin_method(row),
        price=_parse_decimal(row.get("price")),
        rating=_parse_decimal(row.get("rating")),
        attendee_count=int(row.get("attendee_count") or 0),
        # `.get` with defaults: rows predating the capacity migration lack these keys.
        capacity=int(row["capacity"]) if row.get("capacity") is not None else None,
        waitlist_enabled=bool(row.get("waitlist_enabled", True)),
        call_ttl_minutes=int(row.get("call_ttl_minutes") or 10),
        capacity_state=capacity_state,
        is_boosted=bool(row.get("is_boosted", False)),
        boost_expires_at=_parse_dt(row.get("boost_expires_at")),
        created_at=_parse_dt(row.get("created_at")),
        media=media,
        opening_hours=row.get("opening_hours") or None,
        price_level=int(row["price_level"]) if row.get("price_level") is not None else None,
        source_url=row.get("source_url"),
        cover_url=row.get("cover_url"),
        moderation_blocked=bool(row.get("moderation_blocked")),
    )


@router.post("/discover", response_model=list[EventRead])
@limiter.limit("20/minute")
async def discover_events(
    request: Request,
    response: Response,
    body: DiscoverRequest,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> list[EventRead]:
    """Events near the user, best match first.

    Users without an interest vector (no onboarding yet) get the same nearby events
    and places, what pleases most people first, instead of an error. ``X-Discovery-Mode`` tells the app which
    one it got (``personalized`` or ``nearby``) so it can suggest the onboarding.
    """
    user_id, jwt = auth
    profile = await db.get_profile(user_id, jwt)
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Perfil não encontrado.")
    personal = parse_interest_embedding(profile.get("interest_embedding"))
    if personal is None or len(personal) != EMBEDDING_DIMENSIONS:
        # Lazy backfill: rebuild the vector from stored onboarding preferences when we can,
        # so users who onboarded before the pipeline (or hit a transient embedding error)
        # get personalised results instead of the nearby fallback below.
        personal = await ensure_interest_embedding(db, user_id, jwt, profile)
    query_embedding: list[float] | None
    if personal is None or len(personal) != EMBEDDING_DIMENSIONS:
        query_embedding = None
        response.headers[DISCOVERY_MODE_HEADER] = "nearby"
    else:
        response.headers[DISCOVERY_MODE_HEADER] = "personalized"
        query_embedding = personal
        if profile.get("social_mode_enabled", False):
            social_vec = await db.get_social_avg_embedding(user_id)
            if social_vec is not None and len(social_vec) == EMBEDDING_DIMENSIONS:
                query_embedding = ai_service.blend_vectors(personal, social_vec)

    matches = await db.call_match_events(
        query_embedding,
        body.latitude,
        body.longitude,
        body.radius_km,
        kinds=body.kinds,
        categories=body.categories,
        date_from=body.date_from,
        date_to=body.date_to,
        max_price=body.max_price,
    )
    if not matches:
        return []

    # Fetch every matched row (already bounded by the RPC's own limit) so past events are
    # dropped BEFORE pagination — filtering after the page slice would return short pages.
    all_ids = [str(m.get("id")) for m in matches if m.get("id")]
    if not all_ids:
        return []
    rows_by_id = await db.batch_fetch_events_by_ids(all_ids, jwt)
    ordered_rows = [rows_by_id[i] for i in all_ids if i in rows_by_id]
    future_rows = filter_future_events(ordered_rows)

    offset = (body.page - 1) * body.page_size
    page_rows = future_rows[offset : offset + body.page_size]
    if not page_rows:
        return []

    page_ids = [str(row["id"]) for row in page_rows]
    media_map = await db.batch_fetch_event_media(page_ids, jwt)
    return [
        _row_to_event_read(row, media_map.get(str(row["id"]), []))
        for row in page_rows
    ]


def _event_occurrences(body: EventCreate) -> list[datetime]:
    """Start of each event row to create: the single date, or the expanded recurrence.

    Naive Recife wall-clock times (see `_EventFields`).
    """
    if body.recurrence is None:
        return [body.event_date]
    return list(expand_recurrence(body.event_date, body.recurrence))


def _schedule(body: _EventFields, start: datetime) -> dict[str, str]:
    """Start, end and check-in opening of one occurrence, with the Recife offset.

    Every occurrence keeps the event's duration and the same check-in lead time.
    """
    duration = body.ends_at - body.event_date
    opens_before = body.event_date - (body.checkin_opens_at or body.event_date)
    return {
        "event_date": local_iso(start),
        "ends_at": local_iso(start + duration),
        "checkin_opens_at": local_iso(start - opens_before),
    }


def _event_payload(body: _EventFields) -> dict[str, Any]:
    """Columns the creator sets, the same when creating and editing."""
    payload: dict[str, Any] = {
        "title": body.title,
        "description": body.description,
        "category": body.category,
        "location_name": body.location_name,
        "lat": body.lat,
        "long": body.long,
        "place_id": body.place_id,
        "price": str(body.price) if body.price is not None else None,
        # capacity None = unlimited event; the other three only matter once it is set,
        # but storing them unconditionally keeps every occurrence row self-describing.
        "capacity": body.capacity,
        "waitlist_enabled": body.waitlist_enabled,
        "call_ttl_minutes": body.call_ttl_minutes,
        "auto_call_next": body.auto_call_next,
        # Checked by `check_event_content` before this is written (#22 A); events
        # created in the app have no cover, only their own media.
        "moderation_hash": event_moderation_hash(body.title, body.description, None),
        "moderated_at": datetime.now(timezone.utc).isoformat(),
        "moderation_blocked": False,
        "moderation_reasons": None,
    }
    return payload


def _trophy_row(event_id: str, body: _EventFields) -> dict[str, Any]:
    trophy = body.trophy
    return {
        "event_id": event_id,
        "name": (trophy.name if trophy else None) or default_trophy_name(body.title),
        "description": trophy.description if trophy else None,
        "icon_url": trophy.icon_url if trophy else None,
    }


async def _check_uploads_and_place(
    db: DbServiceDep, body: _EventFields, user_id: str, jwt: str
) -> None:
    """Checked before any write: a rejected path must not leave half an event behind."""
    require_media_paths_owned(user_id, (m.media_url for m in body.media))
    if body.trophy is not None and body.trophy.icon_url is not None:
        require_trophy_icon(user_id, body.trophy.icon_url)
    if body.place_id is not None:
        await require_place(db, body.place_id, jwt)


@router.post("/describe", response_model=EventDescribeRead)
@limiter.limit("10/hour")
async def suggest_description(
    request: Request,
    body: EventDescribeRequest,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> EventDescribeRead:
    """ "Melhorar descrição" (#22 C): a suggestion from what the creator wrote, never saved."""
    return EventDescribeRead(description=await describe_event(body))


@router.post("/", response_model=EventRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def create_event(
    request: Request,
    body: EventCreate,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> EventRead:
    user_id, jwt = auth
    await _check_uploads_and_place(db, body, user_id, jwt)
    # Moderation and personal data (#22 A): refused before anything is written.
    await check_event_content(db, body)
    base_payload = _event_payload(body)
    if body.place_id is None:
        del base_payload["place_id"]
    if body.price is None:
        del base_payload["price"]

    occurrences = _event_occurrences(body)
    if not occurrences:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A recorrência não gerou nenhuma data.",
        )
    event_rows = [{**base_payload, **_schedule(body, occ)} for occ in occurrences]

    created = await db.create_events_bulk(user_id, event_rows, jwt)
    if not created:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Não foi possível criar o evento. Tente de novo.",
        )
    all_ids = [str(r["id"]) for r in created if r.get("id")]

    try:
        # Same media set is attached to every occurrence (paths already live in Storage).
        if body.media and all_ids:
            media_rows = [
                {
                    "event_id": eid,
                    "media_url": m.media_url,
                    "type": m.type.value,
                    "order_index": m.order_index,
                }
                for eid in all_ids
                for m in body.media
            ]
            await db.insert_event_media_rows(media_rows, jwt)

        # A custom trophy is written now, one per occurrence (each date is its own
        # event and its own trophy). Without one, check-in creates the default trophy.
        if body.trophy is not None and all_ids:
            await db.create_trophy_templates([_trophy_row(eid, body) for eid in all_ids])
    except Exception:
        # Without this the creator retries and gets duplicate events, the first ones
        # missing their media or trophy. Media, trophies and RSVPs go by cascade.
        logger.exception("Event creation failed after the insert; rolling back %s", all_ids)
        await db.delete_events(all_ids, user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Não foi possível criar o evento. Tente de novo.",
        ) from None

    # Keywords and vector (#22 B), best effort: one per series, the text is shared.
    await embed_app_events(db, all_ids, created[0])

    first = created[0]
    first_id = str(first["id"])
    media_map = await db.batch_fetch_event_media([first_id], jwt)
    return _row_to_event_read(first, media_map.get(first_id, []))


def _my_events_order(rows: list[dict[str, Any]], when: MyEventsWhen | None) -> list[dict[str, Any]]:
    """By date: soonest first, or latest first for past events; undated last."""
    sign = -1 if when == "past" else 1

    def key(row: dict[str, Any]) -> tuple[bool, float]:
        start = parse_aware(row.get("event_date"))
        return (start is None, sign * start.timestamp() if start else 0.0)

    return sorted(rows, key=key)


@router.get("/mine", response_model=list[EventRead])
@limiter.limit("20/minute")
async def list_my_events(
    request: Request,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
    role: MyEventsRole = "created",
    when: MyEventsWhen | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1000),
) -> list[EventRead]:
    """"Meus eventos" (#40): what you created, what you are going to (`role=going`,
    with your RSVP), or both (`role=all`, each event once), optionally only upcoming or
    past events. Without parameters, what you created, newest first, as before."""
    user_id, jwt = auth
    if role == "created" and when is None:
        rows = await db.get_events_by_creator(user_id, jwt, limit=limit, offset=offset, when=None)
    else:
        # Both sources are sorted the same way, so the first `offset + limit` of each
        # hold the page of the merged list.
        wanted = offset + limit
        by_id: dict[str, dict[str, Any]] = {}
        if role in ("created", "all"):
            created = await db.get_events_by_creator(user_id, jwt, limit=wanted, offset=0, when=when)
            by_id.update({str(r["id"]): r for r in created if r.get("id")})
        if role in ("going", "all"):
            for row in await db.get_events_going(user_id, jwt, when=when, limit=wanted):
                by_id[str(row["id"])] = {**by_id.get(str(row["id"]), {}), **row}
        rows = _my_events_order(list(by_id.values()), when)[offset:wanted]
    if not rows:
        return []
    event_ids = [str(r["id"]) for r in rows if r.get("id")]
    media_map = await db.batch_fetch_event_media(event_ids, jwt)
    out: list[EventRead] = []
    for row in rows:
        event = _row_to_event_read(row, media_map.get(str(row["id"]), []))
        rsvp = row.get("_rsvp")
        if isinstance(rsvp, dict) and rsvp.get("status"):
            event.my_rsvp = MyRsvpRead(
                status=RsvpStatus(str(rsvp["status"])), waitlist_position=rsvp.get("waitlist_position")
            )
        out.append(event)
    return out


@router.get("/{place_id}/events", response_model=list[EventRead])
@limiter.limit("30/minute")
async def list_place_events(
    request: Request,
    place_id: str,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> list[EventRead]:
    """Upcoming events at a place (kind = place), soonest first."""
    _, jwt = auth
    place = await db.get_event_by_id(place_id, jwt)
    if not place or place.get("kind") != "place" or place.get("moderation_blocked"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lugar não encontrado.")
    rows = await db.list_place_events(place_id, jwt)
    if not rows:
        return []
    ids = [str(r["id"]) for r in rows if r.get("id")]
    media_map = await db.batch_fetch_event_media(ids, jwt)
    return [_row_to_event_read(row, media_map.get(str(row["id"]), [])) for row in rows]


@router.get("/health")
async def events_health() -> dict[str, str]:
    return {"status": "ok"}


async def _capacity_state(
    db: DbServiceDep,
    event_id: str,
    user_id: str,
    jwt: str,
    row: dict[str, Any],
) -> EventCapacityRead:
    """Capacity snapshot for one event, from the requesting user's point of view.

    Only computed on the single-event route: doing it per row in discovery would
    be three extra round-trips per result.
    """
    await db.sweep_expired_calls(event_id)
    capacity = row.get("capacity")
    taken = await db.count_rsvps_by_status(
        event_id,
        [
            RsvpStatus.confirmed.value,
            RsvpStatus.called.value,
            RsvpStatus.admitted.value,
        ],
        jwt,
    )
    waitlist_count = await db.count_rsvps_by_status(
        event_id, [RsvpStatus.waitlisted.value], jwt
    )
    mine = await db.get_rsvp(event_id, user_id, jwt)
    my_status = RsvpStatus(str(mine["status"])) if mine else None
    return EventCapacityRead(
        capacity=int(capacity) if capacity is not None else None,
        taken=taken,
        waitlist_count=waitlist_count,
        my_status=my_status,
        my_waitlist_position=mine.get("waitlist_position") if mine else None,
    )


@router.get("/{event_id}", response_model=EventRead)
@limiter.limit("30/minute")
async def get_event(
    request: Request,
    event_id: str,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> EventRead:
    """Return a single event by ID."""
    user_id, jwt = auth
    row = await db.get_event_by_id(event_id, jwt)
    # What the moderation refused is not shown, not even by its link (#22 A), except
    # to its creator, who can fix it by editing.
    if not row or (row.get("moderation_blocked") and str(row.get("creator_id")) != user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evento não encontrado.")
    media_map = await db.batch_fetch_event_media([event_id], jwt)
    media = media_map.get(event_id, [])
    capacity_state = await _capacity_state(db, event_id, user_id, jwt, row)
    return _row_to_event_read(row, media, capacity_state)


# ------------------------------------------------------------------ editing --


def _editable(event: dict[str, Any]) -> None:
    """Imported events and places come from a source: a re-import would undo an edit."""
    if event.get("kind") == "place" or event.get("source"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Eventos importados e lugares não se editam no Flyer.",
        )


@router.get("/{event_id}/edit", response_model=EventEditRead)
@limiter.limit("30/minute")
async def get_event_for_edit(
    request: Request,
    event_id: str,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> EventEditRead:
    """The edit form: stored values with Storage paths (creator only)."""
    user_id, jwt = auth
    row = await require_event_creator(db, event_id, user_id, jwt)
    _editable(row)
    media_rows = await db.get_event_media_rows(event_id)
    template = await db.get_trophy_template_by_event(event_id)
    icon = template.get("icon_url") if template else None
    paths = [str(m["media_url"]) for m in media_rows if m.get("media_url")]
    try:
        signed = await db.sign_media_paths([*paths, *([icon] if icon else [])])
    except Exception:
        logger.exception("Signing previews for the edit form failed (event=%s)", event_id)
        signed = {}
    return EventEditRead(
        id=str(row["id"]),
        title=str(row["title"]),
        description=row.get("description"),
        category=row.get("category"),
        place_id=str(row["place_id"]) if row.get("place_id") else None,
        location_name=row.get("location_name"),
        lat=float(row["lat"]),
        long=float(row["long"]),
        event_date=_parse_dt(row.get("event_date")),
        ends_at=_parse_dt(row.get("ends_at")),
        checkin_opens_at=_parse_dt(row.get("checkin_opens_at")),
        price=_parse_decimal(row.get("price")),
        capacity=int(row["capacity"]) if row.get("capacity") is not None else None,
        waitlist_enabled=bool(row.get("waitlist_enabled", True)),
        call_ttl_minutes=int(row.get("call_ttl_minutes") or 10),
        auto_call_next=bool(row.get("auto_call_next", True)),
        media=[
            EventMediaEdit(
                media_url=str(m["media_url"]),
                type=m["type"],
                order_index=int(m.get("order_index") or 0),
                preview_url=signed.get(str(m["media_url"])),
            )
            for m in media_rows
            if m.get("media_url")
        ],
        trophy=TrophyEditRead(
            name=str(template.get("name") or ""),
            description=template.get("description"),
            icon_url=icon,
            icon_preview_url=signed.get(icon) if icon else None,
        )
        if template
        else None,
    )


@router.put("/{event_id}", response_model=EventRead)
@limiter.limit("10/minute")
async def update_event(
    request: Request,
    event_id: str,
    body: EventUpdate,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> EventRead:
    """Edit one event (one date of a series). Only its creator; not imports nor places."""
    user_id, jwt = auth
    current = await require_event_creator(db, event_id, user_id, jwt)
    _editable(current)
    await _check_uploads_and_place(db, body, user_id, jwt)
    await check_event_content(db, body)

    payload = {**_event_payload(body), **_schedule(body, body.event_date)}
    row = await db.update_event(event_id, user_id, payload)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evento não encontrado.")
    await db.replace_event_media(
        event_id,
        [
            {
                "event_id": event_id,
                "media_url": m.media_url,
                "type": m.type.value,
                "order_index": m.order_index,
            }
            for m in body.media
        ],
    )
    # Always written: without a custom trophy the default follows the (new) title.
    await db.upsert_trophy_template(_trophy_row(event_id, body))
    # Only redone when the title, description or category changed.
    await embed_app_events(db, [event_id], row, previous=current)

    media_map = await db.batch_fetch_event_media([event_id], jwt)
    return _row_to_event_read(row, media_map.get(event_id, []))


async def _set_checkin(
    db: DbServiceDep, event_id: str, user_id: str, jwt: str, *, open_now: bool
) -> EventRead:
    event = await require_event_creator(db, event_id, user_id, jwt)
    now = datetime.now(timezone.utc)
    if open_now:
        ends_at = parse_aware(event.get("ends_at"))
        if ends_at is None or ends_at <= now:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="O evento já terminou: altere o horário de término para reabrir o check-in.",
            )
        payload: dict[str, Any] = {"checkin_opens_at": now.isoformat(), "checkin_closed_at": None}
    else:
        payload = {"checkin_closed_at": now.isoformat()}
    row = await db.update_event(event_id, user_id, payload)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evento não encontrado.")
    media_map = await db.batch_fetch_event_media([event_id], jwt)
    return _row_to_event_read(row, media_map.get(event_id, []))


@router.post("/{event_id}/checkin/open", response_model=EventRead)
@limiter.limit("10/minute")
async def open_checkin(
    request: Request,
    event_id: str,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> EventRead:
    """The creator opens check-in now (until the event ends)."""
    user_id, jwt = auth
    return await _set_checkin(db, event_id, user_id, jwt, open_now=True)


@router.post("/{event_id}/checkin/close", response_model=EventRead)
@limiter.limit("10/minute")
async def close_checkin(
    request: Request,
    event_id: str,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> EventRead:
    """The creator closes check-in before the event ends."""
    user_id, jwt = auth
    return await _set_checkin(db, event_id, user_id, jwt, open_now=False)
