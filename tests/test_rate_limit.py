"""Rate limiting that holds across serverless instances (#8).

The counters used to live in each function instance's memory, so a limit of
5/minute was 5/minute per instance. They now go to a shared store when one is
configured, and authenticated requests are counted per user, not per IP.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.core.config import Settings
from app.core.limiter import build_limiter, client_ip, rate_limit_key


def _request(headers: dict[str, str] | None = None, host: str = "10.0.0.1") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": raw, "client": (host, 1234)})


def _fake_decode(token: str, _secret: str) -> dict[str, str]:
    if token.startswith("valid-"):
        return {"sub": token.removeprefix("valid-")}
    raise HTTPException(status_code=401, detail="Could not validate credentials.")


# ------------------------------------------------------------------ keys --


def test_client_ip_comes_from_the_header_vercel_overwrites() -> None:
    # Behind Vercel the socket peer is the proxy; x-forwarded-for is rewritten by
    # the platform, so its first entry is the real client and cannot be spoofed.
    request = _request({"X-Forwarded-For": "203.0.113.7, 10.1.1.1"})

    assert client_ip(request) == "203.0.113.7"


def test_client_ip_falls_back_to_the_socket_peer() -> None:
    assert client_ip(_request(host="198.51.100.2")) == "198.51.100.2"


def test_a_valid_token_is_counted_per_user() -> None:
    with patch("app.core.limiter.decode_access_token", _fake_decode):
        key = rate_limit_key(_request({"Authorization": "Bearer valid-user-1"}))

    assert key == "user:user-1"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer forged"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
    ],
)
def test_anything_else_is_counted_per_ip(headers: dict[str, str]) -> None:
    # A forged token must not let someone spend another user's budget: only a
    # token that verifies earns a per-user key.
    with patch("app.core.limiter.decode_access_token", _fake_decode):
        key = rate_limit_key(_request({**headers, "X-Forwarded-For": "203.0.113.7"}))

    assert key == "ip:203.0.113.7"


# --------------------------------------------------------------- storage --


@pytest.mark.parametrize("name", ["RATE_LIMIT_STORAGE_URI", "REDIS_URL", "KV_URL"])
def test_the_shared_store_is_read_from_the_usual_variable_names(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Upstash via the Vercel Marketplace injects REDIS_URL / KV_URL; an explicit
    # RATE_LIMIT_STORAGE_URI also works.
    for other in ("RATE_LIMIT_STORAGE_URI", "REDIS_URL", "KV_URL"):
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(name, "rediss://default:pw@example.upstash.io:6379")

    assert Settings(_env_file=None).RATE_LIMIT_STORAGE_URI.startswith("rediss://")


def test_without_a_store_it_stays_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("RATE_LIMIT_STORAGE_URI", "REDIS_URL", "KV_URL"):
        monkeypatch.delenv(name, raising=False)

    assert Settings(_env_file=None).RATE_LIMIT_STORAGE_URI == ""


# -------------------------------------------------------------- behaviour --


def _app(storage_uri: str) -> TestClient:
    limiter = build_limiter(storage_uri)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    @app.get("/limited")
    @limiter.limit("2/minute")
    async def limited(request: Request) -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app)


def _call(client: TestClient, user: str | None, ip: str) -> int:
    headers = {"X-Forwarded-For": ip}
    if user:
        headers["Authorization"] = f"Bearer valid-{user}"
    return client.get("/limited", headers=headers).status_code


def test_a_user_keeps_one_budget_across_ips_and_users_do_not_share() -> None:
    client = _app("memory://")
    with patch("app.core.limiter.decode_access_token", _fake_decode):
        assert _call(client, "ana", "203.0.113.1") == 200
        assert _call(client, "ana", "203.0.113.2") == 200
        # Same user from another network: still the same budget.
        assert _call(client, "ana", "203.0.113.3") == 429
        # Someone else is unaffected.
        assert _call(client, "rui", "203.0.113.1") == 200


def test_an_unreachable_store_does_not_take_the_api_down() -> None:
    # Fail open to per-instance memory (the old behaviour) rather than 500 on
    # every request when Redis is down.
    client = _app("redis://127.0.0.1:1")
    with patch("app.core.limiter.decode_access_token", _fake_decode):
        assert _call(client, "ana", "203.0.113.1") == 200
