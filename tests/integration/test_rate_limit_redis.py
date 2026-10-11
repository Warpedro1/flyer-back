"""Two serverless instances share one budget when the store is Redis (#8).

Each Vercel instance builds its own Limiter; with the old in-memory store each
one counted on its own. Skipped unless TEST_REDIS_URL points at a Redis server
(CI starts one; locally: any redis-server).
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.core.limiter import build_limiter

REDIS_URL = os.environ.get("TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(not REDIS_URL, reason="TEST_REDIS_URL not set")


def _fake_decode(token: str, _secret: str) -> dict[str, str]:
    if token.startswith("valid-"):
        return {"sub": token.removeprefix("valid-")}
    raise HTTPException(status_code=401, detail="Could not validate credentials.")


def _instance() -> TestClient:
    """A fresh app + limiter, as a new serverless instance would build them."""
    assert REDIS_URL is not None
    limiter = build_limiter(REDIS_URL)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    @app.get("/limited")
    @limiter.limit("2/minute")
    async def limited(request: Request) -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app)


def test_the_budget_is_shared_between_instances() -> None:
    user = f"user-{uuid.uuid4().hex[:8]}"  # fresh key: Redis outlives the test
    headers = {"Authorization": f"Bearer valid-{user}"}
    first, second = _instance(), _instance()

    with patch("app.core.limiter.decode_access_token", _fake_decode):
        assert first.get("/limited", headers=headers).status_code == 200
        assert second.get("/limited", headers=headers).status_code == 200
        # In memory, each instance would still allow two more.
        assert first.get("/limited", headers=headers).status_code == 429
        assert second.get("/limited", headers=headers).status_code == 429
