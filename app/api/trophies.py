"""Trophy and check-in endpoints."""

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import DbServiceDep, get_current_user_with_token
from app.core.config import settings
from app.core.errors import ApiError
from app.core.geo import haversine_meters
from app.core.limiter import limiter
from app.models.schemas import CheckInRequest, TrophyRead
from app.services.checkin_service import (
    TrophyUnavailable,
    parse_dt,
    checkin_method,
    event_trophy_template,
    sign_icons,
    trophy_read,
)
from app.services.checkin_window import checkin_state, refusal_detail
from app.services.db_service import UniqueViolationError

router = APIRouter(prefix="/trophies", tags=["trophies"])

# What check-in reads from the event: no `select=*`, the row carries a 1536-d embedding.
_CHECKIN_COLUMNS = (
    "id,kind,source,title,lat,long,event_date,ends_at,checkin_opens_at,checkin_closed_at,opening_hours,"
    "moderation_blocked"
)


@router.post("/checkin", response_model=TrophyRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def check_in_trophy(
    request: Request,
    body: CheckInRequest,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> TrophyRead:
    user_id, jwt = auth
    event = await db.get_event_by_id(body.event_id, jwt, select=_CHECKIN_COLUMNS)
    # What the moderation hid (#22 A) gives no trophy.
    if not event or event.get("lat") is None or event.get("long") is None or event.get("moderation_blocked"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evento não encontrado.")
    # GPS check-in is for places only (decisions 2026-10-17).
    method = checkin_method(event)
    if method == "qr":
        raise ApiError(
            409, "checkin_by_qr", "Neste evento, o check-in é feito na entrada: mostre seu ingresso."
        )
    if method == "none":
        raise ApiError(409, "checkin_unavailable", "Este evento é só informativo: não tem check-in.")
    distance = haversine_meters(body.lat, body.long, float(event["lat"]), float(event["long"]))
    if distance > settings.CHECKIN_MAX_DISTANCE_METERS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Você está longe demais do local do evento.",
        )
    # Only while check-in is open (decisions 2026-10-12): otherwise one visit to the
    # venue of a recurring event would collect the trophy of every date.
    state = checkin_state(event, datetime.now(timezone.utc))
    if state.status != "open":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=refusal_detail(state, is_place=event.get("kind") == "place"),
        )

    # Every event gives a trophy (decision 2026-10-12): without a custom one, the
    # default is created on the first check-in.
    try:
        template = await event_trophy_template(db, {**event, "id": body.event_id})
    except TrophyUnavailable:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Não foi possível preparar o troféu deste evento.",
        ) from None

    try:
        trophy_row = await db.insert_trophy(user_id, str(template["id"]), jwt)
    except UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Troféu já resgatado.",
        ) from None

    return await trophy_read(db, template, trophy_row)


@router.get("/", response_model=list[TrophyRead])
@limiter.limit("20/minute")
async def list_user_trophies(
    request: Request,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> list[TrophyRead]:
    """Return all trophies the authenticated user has earned."""
    user_id, jwt = auth
    rows = await db.get_user_trophies(user_id, jwt)
    templates: list[dict[str, Any]] = []
    for row in rows:
        tpl = row.get("trophy_templates") or {}
        if isinstance(tpl, list):
            tpl = tpl[0] if tpl else {}
        templates.append(tpl)
    signed = await sign_icons(db, [t.get("icon_url") for t in templates])
    out: list[TrophyRead] = []
    for row, tpl in zip(rows, templates, strict=True):
        icon = tpl.get("icon_url")
        out.append(
            TrophyRead(
                id=str(row.get("id", "")),
                template_id=str(row.get("template_id", "")),
                name=str(tpl.get("name", "")),
                description=tpl.get("description"),
                icon_url=signed.get(icon) if icon else None,
                acquired_at=parse_dt(row.get("acquired_at")),
                event_id=str(tpl["event_id"]) if tpl.get("event_id") else None,
            )
        )
    return out


@router.get("/health")
async def trophies_health() -> dict[str, str]:
    return {"status": "ok"}
