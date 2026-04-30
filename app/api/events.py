"""Event discovery endpoints."""

import json
from datetime import datetime
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

    offset = (body.page - 1) * body.page_size
    page_slice = matches[offset : offset + body.page_size]
    event_ids = [str(m.get("id")) for m in page_slice if m.get("id")]
    if not event_ids:
        return []

    rows_by_id = await db.batch_fetch_events_by_ids(event_ids, jwt)
    media_map = await db.batch_fetch_event_media(event_ids, jwt)

    out: list[EventRead] = []
    for m in page_slice:
        eid = str(m.get("id", ""))
        if not eid or eid not in rows_by_id:
            continue
        row = rows_by_id[eid]
        media = media_map.get(eid, [])
        out.append(_row_to_event_read(row, media))
    return out


@router.post("/", response_model=EventRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def create_event(
    request: Request,
    body: EventCreate,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> EventRead:
    user_id, jwt = auth
    event_payload: dict[str, Any] = {
        "title": body.title,
        "description": body.description,
        "category": body.category,
        "location_name": body.location_name,
        "lat": body.lat,
        "long": body.long,
    }
    if body.event_date is not None:
        event_payload["event_date"] = body.event_date.isoformat()
    if body.price is not None:
        event_payload["price"] = str(body.price)

    row = await db.create_event(user_id, event_payload, jwt)

    media: list[EventMediaRead] = []
    if body.media:
        media_dicts = [
            {
                "media_url": m.media_url,
                "type": m.type.value,
                "order_index": m.order_index,
            }
            for m in body.media
        ]
        await db.insert_event_media(str(row["id"]), media_dicts, jwt)
        media_map = await db.batch_fetch_event_media([str(row["id"])], jwt)
        media = media_map.get(str(row["id"]), [])

    return _row_to_event_read(row, media)


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
