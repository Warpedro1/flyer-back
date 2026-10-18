"""Pytest bootstrap: minimal env so `app.core.config` can load in CI without a real `.env`."""

from __future__ import annotations

import os

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_KEY", "test-service-role-key")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("PUSHER_APP_ID", "1")
os.environ.setdefault("PUSHER_KEY", "test-pusher-key")
os.environ.setdefault("PUSHER_SECRET", "test-pusher-secret")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-key-for-ci")
os.environ.setdefault("LANGSMITH_TRACING", "false")
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


@pytest.fixture
def recorded() -> Iterator[list[dict[str, Any]]]:
    """Rows sent to `app_errors` (#25 phase 1) through a fake shared client."""
    from app.services import error_log

    rows: list[dict[str, Any]] = []

    async def post(url: str, **kwargs: Any) -> httpx.Response:
        assert url == "/rest/v1/app_errors"
        rows.append(kwargs["json"])
        return httpx.Response(201, request=httpx.Request("POST", "http://x" + url))

    client = MagicMock()
    client.post = AsyncMock(side_effect=post)
    error_log.set_client(client)
    yield rows
    error_log.set_client(None)


class LLMOffInTests(RuntimeError):
    """What the taste agent's LLM answers in a test that did not set it up."""


class _OffRunnable:
    async def ainvoke(self, *_: Any, **__: Any) -> Any:
        raise LLMOffInTests("the taste agent's LLM is off in tests")


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never reach OpenAI: the taste agent's runnables fail unless a test sets
    `taste_agent._structured` itself, so every caller takes its raw-text fallback."""
    from app.services import taste_agent

    monkeypatch.setattr(taste_agent, "_structured", lambda *_, **__: _OffRunnable())
