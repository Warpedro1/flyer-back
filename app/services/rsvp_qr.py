"""Short-lived signed tokens behind the admission QR code.

A QR that carried a stable RSVP id would be a replayable door key: a photo of
someone else's screen would let a stranger walk in as them. Instead the attendee's
phone renders a token that is signed, expires in seconds, and is single-use — the
`jti` is burned on the first successful scan (see `rsvp_admit` in the migration).

The signing key is `RSVP_QR_SECRET`, deliberately separate from
`SUPABASE_JWT_SECRET`: leaking a door key must not leak sessions.

The token also carries where the attendee's phone was when it was issued (claim
`loc`, decisions 2026-10-17): the scan checks they are at the venue. It is rounded
to about 11 m, because anyone who sees the QR can read it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt as pyjwt

from app.core.config import settings
from app.core.errors import ApiError

ALGORITHM = "HS256"

# A phone that reports a worse accuracy than this is not trusted for more slack.
MAX_ACCURACY_METERS = 300.0
# A reading older than this no longer says where the attendee is.
MAX_FIX_AGE_SECONDS = 120.0
_LOC_DECIMALS = 4


@dataclass(frozen=True)
class AttendeePosition:
    """Where the attendee's phone was, and how sure it was (meters)."""

    lat: float
    long: float
    accuracy: float


@dataclass(frozen=True)
class QrClaims:
    """Validated contents of an admission token."""

    rsvp_id: str
    event_id: str
    user_id: str
    jti: str
    position: AttendeePosition | None = None


def _require_secret() -> str:
    secret = (settings.RSVP_QR_SECRET or "").strip()
    if not secret:
        raise ApiError(
            503,
            "qr_disabled",
            "Check-in por QR está desativado (RSVP_QR_SECRET não definido).",
        )
    return secret


def position_from_fix(
    lat: float, long: float, accuracy: float | None, *, fix_age_seconds: float
) -> AttendeePosition | None:
    """A reading from the phone, or None when it is too old (or from the future)."""
    if not -MAX_FIX_AGE_SECONDS < fix_age_seconds < MAX_FIX_AGE_SECONDS:
        return None
    acc = min(max(float(accuracy or 0.0), 0.0), MAX_ACCURACY_METERS)
    return AttendeePosition(lat=float(lat), long=float(long), accuracy=acc)


def issue_token(
    rsvp_id: str, event_id: str, user_id: str, position: AttendeePosition | None = None
) -> tuple[str, datetime]:
    """Mint a token for this RSVP; returns (token, expiry) so the UI can refresh."""
    secret = _require_secret()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.RSVP_QR_TTL_SECONDS)
    claims: dict[str, object] = {
        "rsvp": rsvp_id,
        "evt": event_id,
        "sub": user_id,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    if position is not None:
        claims["loc"] = [
            round(position.lat, _LOC_DECIMALS),
            round(position.long, _LOC_DECIMALS),
            float(round(min(max(position.accuracy, 0.0), MAX_ACCURACY_METERS))),
        ]
    token = pyjwt.encode(claims, secret, algorithm=ALGORITHM)
    return token, expires_at


def _position_claim(value: object) -> AttendeePosition | None:
    if not isinstance(value, list) or len(value) != 3:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        return None
    lat, long, accuracy = (float(v) for v in value)
    if not (-90.0 <= lat <= 90.0 and -180.0 <= long <= 180.0):
        return None
    return AttendeePosition(lat=lat, long=long, accuracy=min(max(accuracy, 0.0), MAX_ACCURACY_METERS))


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
        raise ApiError(401, "qr_expired", "QR expirado. Peça um novo ao participante.") from None
    except pyjwt.InvalidTokenError:
        raise ApiError(401, "qr_invalid", "QR inválido.") from None

    rsvp_id = payload.get("rsvp")
    token_event = payload.get("evt")
    user_id = payload.get("sub")
    jti = payload.get("jti")
    if not all(isinstance(v, str) and v for v in (rsvp_id, token_event, user_id, jti)):
        raise ApiError(401, "qr_invalid", "QR inválido.")

    if token_event != event_id:
        raise ApiError(403, "qr_wrong_event", "Este QR não é deste evento.")

    return QrClaims(
        rsvp_id=rsvp_id,
        event_id=token_event,
        user_id=user_id,
        jti=jti,
        position=_position_claim(payload.get("loc")),
    )
