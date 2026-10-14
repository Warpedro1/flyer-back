"""Accounts by e-mail (decisions 2026-10-12).

* Sign-up: name, e-mail, CPF, mobile and password; CPF, mobile and e-mail are unique.
  Public route (there is no account yet), rate limited per client. The e-mail carries
  a 6-digit code, no link, and the code only counts in the tab that signed up
  (decision 2026-10-14): that tab gets a secret ticket, and the account stays pending
  until `/auth/signup/activate` presents it.
* Second factor: a session opened with a password must verify the 6-digit code that
  Supabase Auth e-mails to the account before using the API. The address always comes
  from the token, never from the client.
* CPF and phone on every account: Google accounts complete them on first access.

`app/api/deps.py` enforces the code and the complete profile on every other route;
the routes here skip exactly the check they exist to satisfy.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.deps import (
    DbServiceDep,
    PendingSession,
    get_current_user_pending_code,
    get_current_user_pending_profile,
    remember_complete_profile,
    remember_verified_session,
)
from app.core.br_documents import mask_cpf, mask_phone
from app.core.limiter import limiter
from app.models.schemas import (
    EmailCodeIn,
    EmailCodeStatus,
    ProfileIdentityIn,
    ProfileIdentityStatus,
    SignupActivateIn,
    SignupIn,
    SignupOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

PendingDep = Annotated[PendingSession, Depends(get_current_user_pending_code)]
PendingProfileDep = Annotated[str, Depends(get_current_user_pending_profile)]

# Said plainly (decision 2026-10-12), in this order when several are taken.
_TAKEN = {
    "email": "Este e-mail já tem uma conta.",
    "cpf": "Este CPF já tem uma conta.",
    "phone": "Este telefone já tem uma conta.",
}


def _conflict(taken: set[str]) -> HTTPException:
    message = " ".join(_TAKEN[field] for field in ("email", "cpf", "phone") if field in taken)
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)


def _ticket_hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode()).hexdigest()


async def _claim_identity(
    db: DbServiceDep, *, email: str | None, cpf: str, phone: str, exclude_user_id: str | None = None
) -> None:
    """409 if an active account has them; sign-ups still waiting for their code give
    way (wrong e-mail, closed tab, lost code) and are deleted."""
    conflict_args: dict[str, str] = {"exclude_user_id": exclude_user_id} if exclude_user_id else {}
    taken = await db.find_identity_conflicts(
        email=email, cpf=cpf, phone=phone, **conflict_args, active_only=True
    )
    if taken:
        raise _conflict(taken)
    for stale_id in await db.find_pending_signups(email=email, cpf=cpf, phone=phone):
        if stale_id != exclude_user_id:
            await db.delete_auth_user(stale_id)


# ----------------------------------------------------------------- sign-up --


@router.post("/signup", status_code=status.HTTP_201_CREATED, response_model=SignupOut)
@limiter.limit("5/10minute")
async def signup(request: Request, body: SignupIn, db: DbServiceDep) -> SignupOut:
    """Create a pending account; Supabase e-mails the 6-digit code.

    Answers the ticket the tab must present with the code (`/auth/signup/activate`).
    Only its hash is stored.
    """
    await _claim_identity(db, email=body.email, cpf=body.cpf, phone=body.phone)
    try:
        user_id = await db.signup_user(
            email=body.email,
            password=body.password,
            data={
                "name": body.name,
                "full_name": body.name,
                "cpf": body.cpf,
                "phone": body.phone,
                # The profile trigger creates the profile as pending.
                "flyer_signup": True,
            },
        )
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == status.HTTP_429_TOO_MANY_REQUESTS:
            raise HTTPException(
                status_code=code, detail="Muitos pedidos seguidos. Espera um minuto e tenta outra vez."
            ) from None
        if code in (400, 422):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Não foi possível criar a conta: verifica o e-mail e a senha.",
            ) from None
        # Two sign-ups with the same CPF or phone at once: the unique index stops the
        # second inside Supabase ("Database error saving new user").
        logger.warning("Supabase sign-up failed (%s)", code)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Este CPF, telefone ou e-mail já tem uma conta.",
        ) from None
    ticket = secrets.token_urlsafe(32)
    await db.create_signup_ticket(user_id, _ticket_hash(ticket))
    return SignupOut(ticket=ticket)


@router.post("/signup/activate", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/10minute")
async def activate_signup(
    request: Request, body: SignupActivateIn, user_id: PendingProfileDep, db: DbServiceDep
) -> Response:
    """Finish a sign-up: the session opened with the code plus the ticket of the tab
    that signed up. The code alone (whoever got the e-mail by mistake) is not enough."""
    if not await db.consume_signup_ticket(user_id, _ticket_hash(body.ticket)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Este código não foi pedido aqui. Abre-o no separador onde fizeste o cadastro, "
            "ou faz o cadastro outra vez.",
        )
    await db.activate_signup(user_id)
    remember_complete_profile(user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------- CPF and phone --


@router.get("/profile/status", response_model=ProfileIdentityStatus)
@limiter.limit("30/minute")
async def profile_status(
    request: Request, user_id: PendingProfileDep, db: DbServiceDep
) -> ProfileIdentityStatus:
    """Whether the account has CPF and phone, with the owner's own masked, and whether
    its sign-up still waits for the code."""
    identity = await db.get_profile_identity(user_id)
    cpf, phone = identity.get("cpf"), identity.get("phone")
    complete = bool(cpf) and bool(phone)
    pending = bool(identity.get("signup_pending"))
    if complete and not pending:
        remember_complete_profile(user_id)
    return ProfileIdentityStatus(
        complete=complete, pending=pending, cpf_masked=mask_cpf(cpf), phone_masked=mask_phone(phone)
    )


@router.put("/profile/identity", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/10minute")
async def complete_profile(
    request: Request, body: ProfileIdentityIn, user_id: PendingProfileDep, db: DbServiceDep
) -> Response:
    """Set CPF and phone, once (Google accounts on first access)."""
    await _claim_identity(db, email=None, cpf=body.cpf, phone=body.phone, exclude_user_id=user_id)
    if not await db.complete_profile_identity(user_id, body.cpf, body.phone):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="O cadastro já está completo: o CPF não se muda por aqui.",
        )
    remember_complete_profile(user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------- e-mail code --


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
