"""Event capacity, waitlist and QR admission.

Split from `events.py` because the queue is its own domain: every write goes
through a Postgres function (see the `20260921000000_event_capacity_waitlist`
migration) so that two people cannot both take the last seat, and the routes here
stay thin wrappers that translate RPC outcomes into HTTP.
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.deps import DbServiceDep, get_current_user_with_token
from app.core.limiter import limiter
from app.models.schemas import (
    AttendeeListRead,
    ProfileBrief,
    RsvpQrToken,
    RsvpRead,
    RsvpStatus,
    RsvpWithProfile,
    ScanRequest,
    ScanResult,
)
from app.services.db_service import RpcError, UniqueViolationError
from app.services.rsvp_qr import issue_token, verify_token

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
            email=profile.get("email"),
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


async def _require_creator(db: DbServiceDep, event_id: str, user_id: str, jwt: str) -> dict[str, Any]:
    """Load the event and reject anyone who is not its creator."""
    event = await db.get_event_by_id(event_id, jwt)
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Evento não encontrado."
        )
    if str(event.get("creator_id") or "") != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Só o criador do evento pode fazer isto.",
        )
    return event


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
    try:
        row = await db.rsvp_join(event_id, user_id)
    except RpcError as exc:
        raise _http_from_rpc(exc) from None
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Não foi possível registar a inscrição.",
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


@router.get("/{event_id}/rsvp/qr", response_model=RsvpQrToken)
@limiter.limit("60/minute")
async def get_rsvp_qr(
    request: Request,
    event_id: str,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> RsvpQrToken:
    """Mint a short-lived admission token for this user's QR code.

    Issued for any active RSVP, including a waitlisted one: the scanner is what
    decides whether the person may enter, and it can then say exactly why not.
    """
    user_id, jwt = auth
    row = await db.get_rsvp(event_id, user_id, jwt)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Sem inscrição neste evento."
        )
    if str(row.get("status")) == RsvpStatus.cancelled.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Esta inscrição foi cancelada."
        )
    token, expires_at = issue_token(str(row["id"]), event_id, user_id)
    return RsvpQrToken(token=token, expires_at=expires_at)


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
    event = await _require_creator(db, event_id, user_id, jwt)
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


@router.post("/{event_id}/attendance/scan", response_model=ScanResult)
@limiter.limit("60/minute")
async def scan_ticket(
    request: Request,
    event_id: str,
    body: ScanRequest,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> ScanResult:
    """Validate an attendee's QR and admit them.

    Signature and expiry are checked before any DB work; the single-use `jti` and
    the eligible-status check happen inside `rsvp_admit` so a replayed token cannot
    slip through between the check and the write.
    """
    user_id, jwt = auth
    await _require_creator(db, event_id, user_id, jwt)
    claims = verify_token(body.token, event_id)
    await db.sweep_expired_calls(event_id)

    try:
        row = await db.rsvp_admit(claims.rsvp_id, event_id, claims.jti)
    except UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Este QR já foi lido."
        ) from None
    except RpcError as exc:
        if exc.message == "rsvp_not_admissible":
            raise await _not_admissible_error(db, claims.rsvp_id, event_id, jwt) from None
        raise _http_from_rpc(exc) from None

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Inscrição não encontrada."
        )

    profile = await db.get_rsvp_profile(claims.user_id, jwt)
    return ScanResult(
        ok=True,
        status=RsvpStatus(str(row["status"])),
        attendee=ProfileBrief(
            id=claims.user_id,
            name=profile.get("name") if profile else None,
            email=profile.get("email") if profile else None,
        ),
    )


async def _not_admissible_error(
    db: DbServiceDep, rsvp_id: str, event_id: str, jwt: str
) -> HTTPException:
    """Turn a generic rejection into the reason the doorkeeper actually needs."""
    row = await db.get_rsvp_by_id(rsvp_id, event_id, jwt)
    current = str(row.get("status")) if row else ""
    if current == RsvpStatus.waitlisted.value:
        detail = "Ainda não é a vez desta pessoa."
    elif current == RsvpStatus.no_show.value:
        detail = "A chamada desta pessoa expirou."
    elif current == RsvpStatus.cancelled.value:
        detail = "Esta pessoa cancelou a inscrição."
    else:
        detail = "Ainda não é a vez desta pessoa."
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
