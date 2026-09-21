"""Short-lived signed tokens behind the admission QR code.

A QR that carried a stable RSVP id would be a replayable door key: a photo of
someone else's screen would let a stranger walk in as them. Instead the attendee's
phone renders a token that is signed, expires in seconds, and is single-use — the
`jti` is burned on the first successful scan (see `rsvp_admit` in the migration).

The signing key is `RSVP_QR_SECRET`, deliberately separate from
`SUPABASE_JWT_SECRET`: leaking a door key must not leak sessions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from fastapi import HTTPException, status

from app.core.config import settings

ALGORITHM = "HS256"


@dataclass(frozen=True)
class QrClaims:
    """Validated contents of an admission token."""

    rsvp_id: str
    event_id: str
    user_id: str
    jti: str


def _require_secret() -> str:
    secret = (settings.RSVP_QR_SECRET or "").strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Check-in por QR está desligado (RSVP_QR_SECRET não definido).",
        )
    return secret


def issue_token(rsvp_id: str, event_id: str, user_id: str) -> tuple[str, datetime]:
    """Mint a token for this RSVP; returns (token, expiry) so the UI can refresh."""
    secret = _require_secret()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.RSVP_QR_TTL_SECONDS)
    token = pyjwt.encode(
        {
            "rsvp": rsvp_id,
            "evt": event_id,
            "sub": user_id,
            "jti": uuid.uuid4().hex,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        },
        secret,
        algorithm=ALGORITHM,
    )
    return token, expires_at


def verify_token(token: str, event_id: str) -> QrClaims:
    """Validate signature, expiry and event binding.

    Raises 401 for a token that is expired or not ours, and 403 for a valid token
    belonging to a different event — a scanner must never admit someone on the
    strength of another event's ticket.
    """
    secret = _require_secret()
    try:
        payload = pyjwt.decode(token, secret, algorithms=[ALGORITHM])
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="QR expirado. Pede um novo ao participante.",
        ) from None
    except pyjwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="QR inválido.",
        ) from None

    rsvp_id = payload.get("rsvp")
    token_event = payload.get("evt")
    user_id = payload.get("sub")
    jti = payload.get("jti")
    if not all(isinstance(v, str) and v for v in (rsvp_id, token_event, user_id, jti)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="QR inválido.",
        )

    if token_event != event_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Este QR não é deste evento.",
        )

    return QrClaims(rsvp_id=rsvp_id, event_id=token_event, user_id=user_id, jti=jti)
