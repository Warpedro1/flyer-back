"""Second factor by e-mail for password logins (decisions 2026-10-12).

A session opened with a password must verify the 6-digit code that Supabase Auth
e-mails to the account before using the API (`app/api/deps.py` enforces it). These
routes authenticate the token without that requirement: they exist to satisfy it.
The address always comes from the token, never from the client.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import (
    DbServiceDep,
    PendingSession,
    get_current_user_pending_code,
    remember_verified_session,
)
from app.core.limiter import limiter

router = APIRouter(prefix="/auth", tags=["auth"])

PendingDep = Annotated[PendingSession, Depends(get_current_user_pending_code)]


class EmailCodeStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool
    verified: bool


class EmailCodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^\d{6}$")


def _require_code_session(session: PendingSession) -> tuple[str, str]:
    if not session.needs_code:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Esta sessão não precisa de código.",
        )
    if not session.email or not session.session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessão inválida: entra outra vez.",
        )
    return session.email, session.session_id


@router.get("/email-code/status", response_model=EmailCodeStatus)
@limiter.limit("30/minute")
async def email_code_status(
    request: Request, session: PendingDep, db: DbServiceDep
) -> EmailCodeStatus:
    """Whether this session still has to verify the e-mail code."""
    if not session.needs_code:
        return EmailCodeStatus(required=False, verified=True)
    verified = bool(session.session_id) and await db.is_session_verified(str(session.session_id))
    if verified:
        remember_verified_session(str(session.session_id))
    return EmailCodeStatus(required=True, verified=verified)


@router.post("/email-code/send", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("3/10minute")
async def send_email_code(request: Request, session: PendingDep, db: DbServiceDep) -> Response:
    """E-mail a 6-digit code to the account's own address."""
    email, _ = _require_code_session(session)
    await db.send_email_code(email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/email-code/verify", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/10minute")
async def verify_email_code(
    request: Request, body: EmailCodeIn, session: PendingDep, db: DbServiceDep
) -> Response:
    """Check the code; on success this session can use the API."""
    email, session_id = _require_code_session(session)
    if not await db.verify_email_code(email, body.code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Código inválido ou expirado.",
        )
    await db.mark_session_verified(session_id, session.user_id)
    remember_verified_session(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
