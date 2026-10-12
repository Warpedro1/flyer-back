"""Trophy and check-in endpoints."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import DbServiceDep, get_current_user_with_token
from app.core.config import settings
from app.core.geo import haversine_meters
from app.core.limiter import limiter
from app.models.schemas import CheckInRequest, TrophyRead
from app.services.db_service import DBService, UniqueViolationError
from app.services.trophies import default_trophy_name

router = APIRouter(prefix="/trophies", tags=["trophies"])


async def _sign_icons(db: DBService, icons: list[str | None]) -> dict[str, str]:
    """Icons are private Storage paths: the app gets short-lived signed URLs."""
    paths = sorted({i for i in icons if i})
    return await db.sign_media_paths(paths) if paths else {}


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


@router.post("/checkin", response_model=TrophyRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def check_in_trophy(
    request: Request,
    body: CheckInRequest,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> TrophyRead:
    user_id, jwt = auth
    event = await db.get_event_by_id(body.event_id, jwt)
    if not event or event.get("lat") is None or event.get("long") is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found.")
    distance = haversine_meters(body.lat, body.long, float(event["lat"]), float(event["long"]))
    if distance > settings.CHECKIN_MAX_DISTANCE_METERS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Too far from event location.",
        )

    # Every event gives a trophy (decision 2026-10-12): without a custom one, the
    # default is created on the first check-in.
    template_id = await db.get_trophy_template_by_event(body.event_id)
    if not template_id:
        template_id = await db.ensure_default_trophy_template(
            body.event_id, default_trophy_name(str(event.get("title") or "evento"))
        )
    if not template_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Não foi possível preparar o troféu deste evento.",
        )

    try:
        trophy_row = await db.insert_trophy(user_id, template_id, jwt)
    except UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Troféu já resgatado.",
        ) from None

    template = await db.get_trophy_template_full(template_id)
    name = str(template.get("name", "")) if template else ""
    description = template.get("description") if template else None
    icon_url = template.get("icon_url") if template else None
    if icon_url:
        icon_url = (await _sign_icons(db, [icon_url])).get(icon_url, icon_url)

    return TrophyRead(
        id=str(trophy_row.get("id", "")),
        template_id=template_id,
        name=name,
        description=description,
        icon_url=icon_url,
        acquired_at=_parse_dt(trophy_row.get("acquired_at")),
    )


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
    signed = await _sign_icons(db, [t.get("icon_url") for t in templates])
    out: list[TrophyRead] = []
    for row, tpl in zip(rows, templates, strict=True):
        icon = tpl.get("icon_url")
        out.append(
            TrophyRead(
                id=str(row.get("id", "")),
                template_id=str(row.get("template_id", "")),
                name=str(tpl.get("name", "")),
                description=tpl.get("description"),
                icon_url=signed.get(icon, icon) if icon else None,
                acquired_at=_parse_dt(row.get("acquired_at")),
            )
        )
    return out


@router.get("/health")
async def trophies_health() -> dict[str, str]:
    return {"status": "ok"}
