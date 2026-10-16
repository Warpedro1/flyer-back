"""Reusable API dependencies."""

from collections import OrderedDict
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.request_context import set_current_user
from app.core.security import decode_access_token, session_needs_email_code
from app.services.db_service import DBService

bearer_scheme = HTTPBearer(auto_error=True)

# `detail` the app recognises to send the user to the e-mail code screen.
EMAIL_CODE_REQUIRED = "email_code_required"
# `detail` the app recognises to send the user to "complete your sign-up" (CPF, phone).
PROFILE_INCOMPLETE = "profile_incomplete"
# `detail` for a sign-up whose code was not entered in the browser that signed up
# (decision 2026-10-14): the app sends the user to the sign-up code screen.
SIGNUP_PENDING = "signup_pending"

# Sessions that already verified the e-mail code, so a password session does not cost
# a database round trip per request. Per instance and bounded: a miss only means one
# more lookup; verification is never undone while the session lives.
_VERIFIED_CACHE_SIZE = 10_000
_verified_sessions: "OrderedDict[str, None]" = OrderedDict()


# Active accounts with CPF and phone (never removed: CPF cannot be unset and an
# activated sign-up does not go back to pending).
_complete_profiles: "OrderedDict[str, None]" = OrderedDict()


def reset_verified_sessions_cache() -> None:
    """For tests."""
    _verified_sessions.clear()
    _complete_profiles.clear()


def _remember(cache: "OrderedDict[str, None]", key: str) -> None:
    cache[key] = None
    cache.move_to_end(key)
    while len(cache) > _VERIFIED_CACHE_SIZE:
        cache.popitem(last=False)


def remember_complete_profile(user_id: str) -> None:
    _remember(_complete_profiles, user_id)


def remember_verified_session(session_id: str) -> None:
    _remember(_verified_sessions, session_id)


async def _session_verified(request: Request, session_id: str) -> bool:
    if session_id in _verified_sessions:
        return True
    # The shared client from the lifespan; only touched for password sessions.
    client = getattr(request.app.state, "http_client", None)
    verified = await DBService(client).is_session_verified(session_id)
    if verified:
        remember_verified_session(session_id)
    return verified


async def _account_state(request: Request, user_id: str) -> str:
    """`ok`, `incomplete` (no CPF/phone) or `pending` (sign-up without its code)."""
    if user_id in _complete_profiles:
        return "ok"
    client = getattr(request.app.state, "http_client", None)
    state = await DBService(client).get_account_state(user_id)
    if state == "ok":
        remember_complete_profile(user_id)
    return state


async def _authenticated_payload(
    request: Request, token: str, *, require_profile: bool = True
) -> dict[str, Any]:
    """The validated JWT payload, after the account checks (decisions 2026-10-12).

    1. A password session only reaches the API after the 6-digit code sent to the
       account's e-mail.
    2. A sign-up by e-mail only counts once its code was entered in the browser that
       signed up (ticket, decision 2026-10-14): a Supabase session opened with the
       code alone, by whoever got the e-mail by mistake, stays out.
    3. Every account needs CPF and phone (Google ones complete them on first access).

    Enforced here and not in the app: whoever has a token can call the API directly
    and skip any screen.
    """
    payload = decode_access_token(token, settings.SUPABASE_JWT_SECRET)
    set_current_user(str(payload["sub"]))
    if session_needs_email_code(payload):
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not await _session_verified(request, session_id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=EMAIL_CODE_REQUIRED)
    if require_profile:
        state = await _account_state(request, payload["sub"])
        if state == "pending":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=SIGNUP_PENDING)
        if state != "ok":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PROFILE_INCOMPLETE)
    return payload


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> str:
    """Return authenticated user identifier extracted from JWT `sub`."""

    payload = await _authenticated_payload(request, credentials.credentials)
    return payload["sub"]


async def get_current_user_with_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> tuple[str, str]:
    """Return (user_id, raw_jwt) for routes that forward the JWT to Supabase (RLS)."""

    payload = await _authenticated_payload(request, credentials.credentials)
    return payload["sub"], credentials.credentials


@dataclass(frozen=True)
class PendingSession:
    """A valid token whose e-mail code may still be missing."""

    user_id: str
    email: str | None
    session_id: str | None
    needs_code: bool


async def get_current_user_pending_code(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
) -> PendingSession:
    """Authenticate the token without requiring the e-mail code.

    Only for the `/auth/email-code/*` routes, which exist to verify it
    (`tests/test_route_auth.py` refuses it anywhere else).
    """
    payload = decode_access_token(credentials.credentials, settings.SUPABASE_JWT_SECRET)
    set_current_user(str(payload["sub"]))
    email = payload.get("email")
    session_id = payload.get("session_id")
    return PendingSession(
        user_id=payload["sub"],
        email=email if isinstance(email, str) and email else None,
        session_id=session_id if isinstance(session_id, str) and session_id else None,
        needs_code=session_needs_email_code(payload),
    )


async def get_current_user_pending_profile(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
) -> str:
    """Authenticate (with the e-mail code) without the account checks.

    Skips CPF/phone and the pending sign-up. Only for the `/auth/profile/*` routes,
    which exist to complete them, and `POST /auth/signup/activate`
    (`tests/test_route_auth.py` refuses it anywhere else).
    """
    payload = await _authenticated_payload(request, credentials.credentials, require_profile=False)
    return str(payload["sub"])


async def get_http_client(request: Request) -> httpx.AsyncClient:
    """Shared AsyncClient from app lifespan (singleton)."""

    return request.app.state.http_client


HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]


def get_db_service(client: HttpClientDep) -> DBService:
    return DBService(client)


DbServiceDep = Annotated[DBService, Depends(get_db_service)]
