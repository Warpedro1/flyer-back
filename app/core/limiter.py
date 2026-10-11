"""Shared SlowAPI limiter instance (avoids circular imports with routers).

Counters go to the shared store in ``RATE_LIMIT_STORAGE_URI`` (Redis/Upstash) so a
limit holds across serverless instances (#8). Requests with a valid token are
counted per user; everything else per client IP.
"""

from fastapi import HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings
from app.core.security import decode_access_token


def client_ip(request: Request) -> str:
    """The caller's IP. Vercel overwrites ``x-forwarded-for``, so it cannot be spoofed."""
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    return first or get_remote_address(request)


def rate_limit_key(request: Request) -> str:
    """``user:<id>`` for a token that verifies, ``ip:<addr>`` otherwise.

    Only a verified token earns a per-user key: trusting an unverified ``sub``
    would let anyone exhaust someone else's budget with a forged token.
    """
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        try:
            payload = decode_access_token(token, settings.SUPABASE_JWT_SECRET)
            return f"user:{payload['sub']}"
        except (HTTPException, KeyError):
            pass
    return f"ip:{client_ip(request)}"


def build_limiter(storage_uri: str) -> Limiter:
    """Limiter on the given store; falls back to per-instance memory if it is down.

    Failing open keeps the API up when Redis is unreachable, at the cost of the
    limits counting per instance until it is back (the behaviour before #8).
    """
    return Limiter(
        key_func=rate_limit_key,
        storage_uri=storage_uri or "memory://",
        in_memory_fallback_enabled=True,
    )


limiter = build_limiter(settings.RATE_LIMIT_STORAGE_URI)
