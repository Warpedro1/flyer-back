"""Security utilities for token validation using Supabase JWKS (ES256)."""

import logging
from typing import Any

import jwt as pyjwt
from fastapi import HTTPException, status
from jwt import PyJWKClient

from app.core.config import settings

logger = logging.getLogger(__name__)

_jwks_url = f"{settings.SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json"
_jwk_client = PyJWKClient(_jwks_url, cache_keys=True, lifespan=3600)


def decode_access_token(token: str, secret: str) -> dict[str, Any]:
    """Decode and validate a Supabase JWT access token.

    Tries ES256 via JWKS first (modern Supabase projects).
    Falls back to HS256 with the provided secret for older projects.
    The ``secret`` parameter is kept for backward compatibility.
    """

    # --- Try asymmetric verification via JWKS (ES256 / RS256) ---
    try:
        signing_key = _jwk_client.get_signing_key_from_jwt(token)
        payload: dict[str, Any] = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256", "RS256", "EdDSA"],
            options={"verify_aud": False},
        )
        return _validate_subject(payload)
    except pyjwt.ExpiredSignatureError:
        logger.warning("JWT expired (JWKS)")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except (pyjwt.PyJWKClientError, pyjwt.DecodeError, pyjwt.InvalidTokenError) as exc:
        logger.info("JWKS verification failed (%s), trying HS256 fallback", exc)

    # --- Fallback: HS256 with symmetric secret ---
    try:
        payload = pyjwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
        return _validate_subject(payload)
    except pyjwt.ExpiredSignatureError:
        logger.warning("JWT expired (HS256)")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except pyjwt.InvalidTokenError as exc:
        logger.warning("JWT decode failed on both JWKS and HS256: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def _validate_subject(payload: dict[str, Any]) -> dict[str, Any]:
    subject = payload.get("sub")
    if not subject or not isinstance(subject, str):
        logger.warning("JWT missing 'sub' claim")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload
