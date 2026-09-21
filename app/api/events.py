"""Event discovery endpoints."""

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import DbServiceDep, get_current_user_with_token
from app.core.limiter import limiter
from app.models.schemas import (
    EMBEDDING_DIMENSIONS,
    DiscoverRequest,
    EventCreate,
    EventMediaRead,
    EventRead,
)
from app.services.ai_service import ai_service
from app.services.event_recurrence import expand_recurrence
from app.services.onboarding_pipeline import ensure_interest_embedding

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])


def _parse_interest_embedding(raw: Any) -> list[float] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        return [float(x) for x in raw]
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            return [float(x) for x in json.loads(text)]
    return None


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def filter_future_events(rows_in_order: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop events whose `event_date` is in the past; keep undated events and preserve order.

    Undated events (`event_date IS NULL`) stay visible on purpose. Naive datetimes are
    treated as UTC so the comparison is consistent regardless of how Postgres serializes them.
    """
    now = datetime.now(timezone.utc)
    kept: list[dict[str, Any]] = []
    for row in rows_in_order:
        dt = _parse_dt(row.get("event_date"))
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


def _row_to_event_read(row: dict[str, Any], media: list[EventMediaRead]) -> EventRead:
    return EventRead(
        id=str(row["id"]),
        creator_id=str(row["creator_id"]) if row.get("creator_id") is not None else None,
        title=str(row["title"]),
        description=row.get("description"),
        category=row.get("category"),
        location_name=row.get("location_name"),
        lat=float(row["lat"]),
        long=float(row["long"]),
        event_date=_parse_dt(row.get("event_date")),
        price=_parse_decimal(row.get("price")),
        rating=_parse_decimal(row.get("rating")),
        attendee_count=int(row.get("attendee_count") or 0),
        is_boosted=bool(row.get("is_boosted", False)),
        boost_expires_at=_parse_dt(row.get("boost_expires_at")),
        created_at=_parse_dt(row.get("created_at")),
        media=media,
    )


@router.post("/discover", response_model=list[EventRead])
@limiter.limit("20/minute")
async def discover_events(
    request: Request,
    body: DiscoverRequest,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> list[EventRead]:
    user_id, jwt = auth
    profile = await db.get_profile(user_id, jwt)
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")
    personal = _parse_interest_embedding(profile.get("interest_embedding"))
    if personal is None or len(personal) != EMBEDDING_DIMENSIONS:
        # Lazy backfill: rebuild the vector from stored onboarding preferences when we can,
        # so users who onboarded before the pipeline (or hit a transient embedding error)
        # aren't permanently stuck on the 400 below.
        personal = await ensure_interest_embedding(db, user_id, jwt, profile)
    if personal is None or len(personal) != EMBEDDING_DIMENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Profile has no interest vector yet. Chat first.",
        )

    social_on = bool(profile.get("social_mode_enabled", False))
    if social_on:
        social_vec = await db.get_social_avg_embedding(user_id)
        if social_vec is not None and len(social_vec) == EMBEDDING_DIMENSIONS:
            query_embedding = ai_service.blend_vectors(personal, social_vec)
        else:
            query_embedding = personal
    else:
        query_embedding = personal

    matches = await db.call_match_events(
        query_embedding,
        body.latitude,
        body.longitude,
        body.radius_km,
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


def _event_occurrences(body: EventCreate) -> list[datetime | None]:
    """One entry per event row to create: the single date, or the expanded recurrence."""
    if body.recurrence is None:
        return [body.event_date]
    return list(expand_recurrence(body.event_date, body.recurrence))


async def _embed_events_best_effort(
    db: DbServiceDep,
    event_ids: list[str],
    body: EventCreate,
) -> None:
    """Generate one embedding from the event's text and attach it to every occurrence.

    Best-effort: a missing/failing embedding provider must not fail event creation
    (the event is simply less discoverable until re-embedded).
    """
    if not event_ids:
        return
    text = " ".join(
        part for part in (body.title, body.description or "", body.category or "") if part
    ).strip()
    if not text:
        return
    try:
        vec = await ai_service.generate_embedding(text)
        if vec:
            await db.set_events_embedding(event_ids, vec)
    except Exception:
        logger.warning("Event embedding failed (ids=%s)", event_ids, exc_info=True)


@router.post("/", response_model=EventRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def create_event(
    request: Request,
    body: EventCreate,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> EventRead:
    user_id, jwt = auth
    base_payload: dict[str, Any] = {
        "title": body.title,
        "description": body.description,
        "category": body.category,
        "location_name": body.location_name,
        "lat": body.lat,
        "long": body.long,
    }
    if body.price is not None:
        base_payload["price"] = str(body.price)

    occurrences = _event_occurrences(body)
    if not occurrences:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Recurrence produced no occurrences.",
        )

    event_rows: list[dict[str, Any]] = []
    for occ in occurrences:
        row = dict(base_payload)
        if occ is not None:
            row["event_date"] = occ.isoformat()
        event_rows.append(row)

    created = await db.create_events_bulk(user_id, event_rows, jwt)
    if not created:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create event.",
        )
    all_ids = [str(r["id"]) for r in created if r.get("id")]

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

    await _embed_events_best_effort(db, all_ids, body)

    first = created[0]
    first_id = str(first["id"])
    media_map = await db.batch_fetch_event_media([first_id], jwt)
    return _row_to_event_read(first, media_map.get(first_id, []))


@router.get("/mine", response_model=list[EventRead])
@limiter.limit("20/minute")
async def list_my_events(
    request: Request,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> list[EventRead]:
    """Return events created by the authenticated user."""
    user_id, jwt = auth
    rows = await db.get_events_by_creator(user_id, jwt)
    if not rows:
        return []
    event_ids = [str(r["id"]) for r in rows if r.get("id")]
    media_map = await db.batch_fetch_event_media(event_ids, jwt)
    return [
        _row_to_event_read(row, media_map.get(str(row["id"]), []))
        for row in rows
    ]


@router.get("/health")
async def events_health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/{event_id}", response_model=EventRead)
@limiter.limit("30/minute")
async def get_event(
    request: Request,
    event_id: str,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> EventRead:
    """Return a single event by ID."""
    _user_id, jwt = auth
    row = await db.get_event_by_id(event_id, jwt)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found.")
    media_map = await db.batch_fetch_event_media([event_id], jwt)
    media = media_map.get(event_id, [])
    return _row_to_event_read(row, media)
