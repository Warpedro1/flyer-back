"""Event capacity, waitlist and QR admission.

Split from `events.py` because the queue is its own domain: every write goes
through a Postgres function (see the `20260921000000_event_capacity_waitlist`
migration) so that two people cannot both take the last seat, and the routes here
stay thin wrappers that translate RPC outcomes into HTTP.
"""

import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.authz import require_event_creator, require_not_place, require_owner
from app.api.deps import DbServiceDep, get_current_user_with_token
from app.core.config import settings
from app.core.errors import ApiError
from app.core.geo import haversine_meters
from app.core.limiter import limiter
from app.core.request_context import route_of
from app.models.schemas import (
    AttendeeListRead,
    ProfileBrief,
    RsvpQrRequest,
    RsvpQrToken,
    RsvpRead,
    RsvpStatus,
    RsvpWithProfile,
    ScanRequest,
    ScanResult,
    TrophyRead,
)
from app.services import error_log, realtime
from app.services.checkin_service import award_checkin_trophy, checkin_method
from app.core.tz import LOCAL_TZ
from app.services.checkin_window import checkin_state
from app.services.db_service import RpcError, UniqueViolationError
from app.services.rsvp_qr import AttendeePosition, issue_token, position_from_fix, verify_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["rsvp"])

# PL/pgSQL RAISE message -> (HTTP status, user-facing detail).
_RPC_ERRORS: dict[str, tuple[int, str]] = {
    "event_not_found": (status.HTTP_404_NOT_FOUND, "Evento não encontrado."),
    "event_full": (status.HTTP_409_CONFLICT, "Evento esgotado."),
    "not_event_creator": (
        status.HTTP_403_FORBIDDEN,
        "Só o criador do evento pode fazer isto.",
    ),
    "rsvp_not_found": (status.HTTP_404_NOT_FOUND, "Inscrição não encontrada."),
    "rsvp_not_recallable": (
        status.HTTP_409_CONFLICT,
        "Esta inscrição não pode ser rechamada.",
    ),
    "already_admitted": (status.HTTP_409_CONFLICT, "Esta pessoa já entrou."),
    "rsvp_not_admissible": (
        status.HTTP_409_CONFLICT,
        "Ainda não é a vez desta pessoa.",
    ),
}


def _http_from_rpc(exc: RpcError) -> HTTPException:
    code, detail = _RPC_ERRORS.get(
        exc.message,
        (status.HTTP_500_INTERNAL_SERVER_ERROR, "Erro ao processar a inscrição."),
    )
    return HTTPException(status_code=code, detail=detail)


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def _row_to_rsvp(row: dict[str, Any], ahead_count: int | None = None) -> RsvpRead:
    return RsvpRead(
        id=str(row["id"]),
        event_id=str(row["event_id"]),
        user_id=str(row["user_id"]),
        status=RsvpStatus(str(row["status"])),
        waitlist_position=row.get("waitlist_position"),
        ahead_count=ahead_count,
        called_at=_parse_dt(row.get("called_at")),
        call_expires_at=_parse_dt(row.get("call_expires_at")),
        admitted_at=_parse_dt(row.get("admitted_at")),
        created_at=_parse_dt(row.get("created_at")),
    )


def _row_to_rsvp_with_profile(row: dict[str, Any]) -> RsvpWithProfile:
    profile = row.get("profiles") or {}
    if isinstance(profile, list):
        profile = profile[0] if profile else {}
    base = _row_to_rsvp(row)
    return RsvpWithProfile(
        **base.model_dump(),
        profile=ProfileBrief(
            id=str(profile.get("id") or row["user_id"]),
            name=profile.get("name"),
        ),
    )


def bucket_attendees(
    rows: list[dict[str, Any]], capacity: Any
) -> AttendeeListRead:
    """Split the queue into the sections the creator's screen shows.

    `taken` counts seats actually held — confirmed, called and admitted. People on
    the waitlist or marked no-show hold nothing, which is what lets the queue keep
    moving past someone who never turned up.
    """
    buckets: dict[str, list[RsvpWithProfile]] = {
        RsvpStatus.confirmed.value: [],
        RsvpStatus.called.value: [],
        RsvpStatus.waitlisted.value: [],
        RsvpStatus.no_show.value: [],
    }
    admitted = 0
    for row in rows:
        current = str(row.get("status"))
        if current == RsvpStatus.admitted.value:
            admitted += 1
            continue
        bucket = buckets.get(current)
        if bucket is not None:
            bucket.append(_row_to_rsvp_with_profile(row))

    taken = (
        len(buckets[RsvpStatus.confirmed.value])
        + len(buckets[RsvpStatus.called.value])
        + admitted
    )
    return AttendeeListRead(
        capacity=int(capacity) if capacity is not None else None,
        taken=taken,
        confirmed=buckets[RsvpStatus.confirmed.value],
        called=buckets[RsvpStatus.called.value],
        waitlist=buckets[RsvpStatus.waitlisted.value],
        no_show=buckets[RsvpStatus.no_show.value],
    )


async def _with_ahead_count(
    db: DbServiceDep, row: dict[str, Any], jwt: str
) -> RsvpRead:
    """Attach the live queue distance for someone still waiting."""
    position = row.get("waitlist_position")
    if str(row.get("status")) == RsvpStatus.waitlisted.value and position is not None:
        ahead = await db.count_waitlist_ahead(str(row["event_id"]), int(position), jwt)
        return _row_to_rsvp(row, ahead_count=ahead)
    return _row_to_rsvp(row)


# ------------------------------------------------------------------ attendee --


@router.post("/{event_id}/rsvp", response_model=RsvpRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("20/minute")
async def join_event(
    request: Request,
    event_id: str,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> RsvpRead:
    """Confirm attendance, or join the waitlist when the event is full.

    Idempotent: an existing active RSVP comes back unchanged instead of erroring.
    """
    user_id, jwt = auth
    event = await db.get_event_by_id(event_id, jwt)
    if event:
        require_not_place(event)
    try:
        row = await db.rsvp_join(event_id, user_id)
    except RpcError as exc:
        raise _http_from_rpc(exc) from None
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Não foi possível registrar a inscrição.",
        )
    return await _with_ahead_count(db, row, jwt)


@router.delete("/{event_id}/rsvp", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("20/minute")
async def leave_event(
    request: Request,
    event_id: str,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> Response:
    """Drop out. A freed seat promotes the head of the waitlist in the same transaction."""
    user_id, _jwt = auth
    try:
        await db.rsvp_cancel(event_id, user_id)
    except RpcError as exc:
        raise _http_from_rpc(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{event_id}/rsvp", response_model=RsvpRead)
@limiter.limit("60/minute")
async def get_my_rsvp(
    request: Request,
    event_id: str,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> RsvpRead:
    """This user's RSVP. 404 when they never joined — not an error for the UI."""
    user_id, jwt = auth
    # Read-through sweep: the caller may be the person whose call just expired.
    await db.sweep_expired_calls(event_id)
    row = await db.get_rsvp(event_id, user_id, jwt)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Sem inscrição neste evento."
        )
    return await _with_ahead_count(db, row, jwt)


def _ticket_unavailable() -> ApiError:
    return ApiError(
        409, "ticket_unavailable", "Este evento é só informativo: não tem ingresso nem check-in."
    )


async def _issue_qr(
    db: DbServiceDep, event_id: str, user_id: str, jwt: str, position: AttendeePosition | None
) -> RsvpQrToken:
    event = await db.get_event_by_id(event_id, jwt, select="id,kind,source")
    if event and checkin_method(event) != "qr":
        raise _ticket_unavailable()
    row = await db.get_rsvp(event_id, user_id, jwt)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Sem inscrição neste evento."
        )
    if str(row.get("status")) == RsvpStatus.cancelled.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Esta inscrição foi cancelada."
        )
    token, expires_at = issue_token(str(row["id"]), event_id, user_id, position)
    return RsvpQrToken(token=token, expires_at=expires_at)


@router.post("/{event_id}/rsvp/qr", response_model=RsvpQrToken)
@limiter.limit("60/minute")
async def issue_rsvp_qr(
    request: Request,
    event_id: str,
    body: RsvpQrRequest,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> RsvpQrToken:
    """Mint a short-lived admission token, carrying where the attendee's phone is.

    Issued for any active RSVP, including a waitlisted one: the scanner is what
    decides whether the person may enter, and it can then say exactly why not.
    """
    user_id, jwt = auth
    position = None
    if body.lat is not None and body.long is not None:
        now = datetime.now(timezone.utc)
        taken = body.fix_at or now
        if taken.tzinfo is None:
            taken = taken.replace(tzinfo=LOCAL_TZ)
        position = position_from_fix(
            body.lat, body.long, body.accuracy, fix_age_seconds=(now - taken).total_seconds()
        )
    return await _issue_qr(db, event_id, user_id, jwt, position)


@router.get("/{event_id}/rsvp/qr", response_model=RsvpQrToken)
@limiter.limit("60/minute")
async def get_rsvp_qr(
    request: Request,
    event_id: str,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> RsvpQrToken:
    """The same token without a position (kept for the app before 2026-10-17)."""
    user_id, jwt = auth
    return await _issue_qr(db, event_id, user_id, jwt, None)


# ------------------------------------------------------------------- creator --


@router.get("/{event_id}/attendees", response_model=AttendeeListRead)
@limiter.limit("60/minute")
async def list_attendees(
    request: Request,
    event_id: str,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> AttendeeListRead:
    """Full queue for the creator. Polling this is what drives the lazy sweep."""
    user_id, jwt = auth
    event = await require_event_creator(db, event_id, user_id, jwt)
    await db.sweep_expired_calls(event_id)

    rows = await db.get_event_rsvps(event_id, jwt)
    return bucket_attendees(rows, event.get("capacity"))


@router.post("/{event_id}/waitlist/call-next", response_model=RsvpRead)
@limiter.limit("30/minute")
async def call_next_in_waitlist(
    request: Request,
    event_id: str,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> RsvpRead | Response:
    """Call the first person in the queue. 204 when there is nobody waiting."""
    user_id, jwt = auth
    try:
        row = await db.rsvp_call_next(event_id, user_id)
    except RpcError as exc:
        raise _http_from_rpc(exc) from None
    if row is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return _row_to_rsvp(row)


@router.post("/{event_id}/waitlist/{rsvp_id}/recall", response_model=RsvpRead)
@limiter.limit("30/minute")
async def recall_attendee(
    request: Request,
    event_id: str,
    rsvp_id: str,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> RsvpRead:
    """Give a no-show a fresh call window — they turned up after the deadline."""
    user_id, _jwt = auth
    try:
        row = await db.rsvp_recall(event_id, rsvp_id, user_id)
    except RpcError as exc:
        raise _http_from_rpc(exc) from None
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Inscrição não encontrada."
        )
    return _row_to_rsvp(row)


# What the scan reads from the event: no `select=*`, the row carries a 1536-d embedding.
_SCAN_COLUMNS = (
    "id,kind,source,creator_id,title,lat,long,event_date,ends_at,"
    "checkin_opens_at,checkin_closed_at,opening_hours"
)

# Why someone with a genuine QR cannot enter, by the status of their RSVP.
_NOT_ADMISSIBLE: dict[str, tuple[str, str]] = {
    RsvpStatus.waitlisted.value: ("not_your_turn", "Ainda não é a vez desta pessoa."),
    RsvpStatus.no_show.value: ("call_expired", "A chamada desta pessoa expirou."),
    RsvpStatus.cancelled.value: ("rsvp_cancelled", "Esta pessoa cancelou a inscrição."),
    RsvpStatus.admitted.value: ("already_admitted", "Esta pessoa já entrou."),
}
_ADMISSIBLE = {RsvpStatus.confirmed.value, RsvpStatus.called.value}


def _not_admissible(rsvp_status: str) -> ApiError:
    code, message = _NOT_ADMISSIBLE.get(rsvp_status, _NOT_ADMISSIBLE[RsvpStatus.waitlisted.value])
    return ApiError(409, code, message)


def _rsvp_not_found() -> ApiError:
    return ApiError(404, "rsvp_not_found", "Inscrição não encontrada.")


def _require_window_open(event: dict[str, Any]) -> None:
    state = checkin_state(event, datetime.now(timezone.utc))
    if state.status == "open":
        return
    if state.status == "not_yet" and state.opens_at is not None:
        local = state.opens_at.astimezone(LOCAL_TZ)
        raise ApiError(
            409, "checkin_not_open", f"O check-in abre às {local:%H:%M} de {local:%d/%m}."
        )
    raise ApiError(409, "checkin_closed", "O check-in deste evento já fechou.")


def _location_checked(
    event: dict[str, Any], position: AttendeePosition | None, name: str, *, confirmed: bool
) -> bool:
    """True when the attendee is at the venue; raise when too far or unknown.

    The limit is the GPS check-in distance plus the accuracy the phone reported
    (already capped), so a weak signal indoors is not turned away at the door.
    """
    if event.get("lat") is None or event.get("long") is None:
        return False
    if position is None:
        if confirmed:
            return False
        raise ApiError(409, "location_missing", f"Não recebemos a localização de {name}.")
    distance = haversine_meters(
        position.lat, position.long, float(event["lat"]), float(event["long"])
    )
    if distance > settings.CHECKIN_MAX_DISTANCE_METERS + position.accuracy:
        raise ApiError(
            403, "attendee_too_far", f"{name} não está no local do evento (a {round(distance)} m)."
        )
    return True


async def _give_trophy(
    request: Request, db: DbServiceDep, event: dict[str, Any], user_id: str, jwt: str
) -> TrophyRead | None:
    """The check-in's trophy; a failure never undoes the entry (it is recorded)."""
    try:
        return await award_checkin_trophy(db, event, user_id, jwt)
    except Exception as exc:  # noqa: BLE001 — the person is already in
        logger.exception("Check-in trophy failed for event %s", event.get("id"))
        await error_log.record_error(
            source="api",
            code="checkin_trophy_failed",
            status=200,
            error_type=type(exc).__name__,
            message=str(exc),
            method=request.method,
            route=route_of(request.scope),
        )
        return None


async def _tell_attendee(user_id: str, event_id: str, trophy: TrophyRead | None) -> None:
    payload: dict[str, Any] = {
        "event_id": event_id,
        "trophy": trophy.model_dump(mode="json") if trophy else None,
    }
    try:
        await realtime.publish(user_id, "checkin:done", payload)
    except Exception:  # noqa: BLE001 — publish is best effort; this is belt and braces
        logger.warning("Realtime notice failed for event %s", event_id)


@router.post("/{event_id}/attendance/scan", response_model=ScanResult)
@limiter.limit("60/minute")
async def scan_ticket(
    request: Request,
    event_id: str,
    body: ScanRequest,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> ScanResult:
    """Read an attendee's QR at the door: this is their check-in (decisions 2026-10-17).

    Window, RSVP status and the attendee's position are checked before anything is
    written, so a refusal leaves the QR usable once the problem is fixed. The
    single-use `jti` is burned inside `rsvp_admit`, so a replayed token cannot slip
    through between the check and the write.
    """
    user_id, jwt = auth
    event = await db.get_event_by_id(event_id, jwt, select=_SCAN_COLUMNS)
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evento não encontrado.")
    require_owner(event.get("creator_id"), user_id, detail="Só o criador do evento pode fazer isto.")
    if checkin_method(event) != "qr":
        raise _ticket_unavailable()
    claims = verify_token(body.token, event_id)
    _require_window_open(event)

    await db.sweep_expired_calls(event_id)
    current = await db.get_rsvp_by_id(claims.rsvp_id, event_id, jwt)
    if current is None:
        raise _rsvp_not_found()
    current_status = str(current.get("status"))
    if current_status == RsvpStatus.admitted.value:
        # A trophy that failed on the first scan is given now.
        await _give_trophy(request, db, event, claims.user_id, jwt)
    if current_status not in _ADMISSIBLE:
        raise _not_admissible(current_status)

    profile = await db.get_rsvp_profile(claims.user_id, jwt)
    name = (profile.get("name") if profile else None) or "Participante"
    location_checked = _location_checked(
        event, claims.position, name, confirmed=body.confirm_without_location
    )

    try:
        row = await db.rsvp_admit(claims.rsvp_id, event_id, claims.jti)
    except UniqueViolationError:
        raise ApiError(409, "qr_already_used", "Este QR já foi lido.") from None
    except RpcError as exc:
        if exc.message == "already_admitted":
            raise _not_admissible(RsvpStatus.admitted.value) from None
        if exc.message == "rsvp_not_found":
            raise _rsvp_not_found() from None
        if exc.message == "rsvp_not_admissible":
            # Raced with the sweep: re-read to say why.
            again = await db.get_rsvp_by_id(claims.rsvp_id, event_id, jwt)
            again_status = str(again.get("status")) if again else ""
            raise _not_admissible(again_status) from None
        raise _http_from_rpc(exc) from None
    if row is None:
        raise _rsvp_not_found()

    trophy = await _give_trophy(request, db, event, claims.user_id, jwt)
    await _tell_attendee(claims.user_id, event_id, trophy)
    return ScanResult(
        ok=True,
        status=RsvpStatus(str(row["status"])),
        attendee=ProfileBrief(id=claims.user_id, name=profile.get("name") if profile else None),
        trophy=trophy,
        location_checked=location_checked,
    )
