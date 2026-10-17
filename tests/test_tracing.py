"""LangSmith traces reach LangSmith from a serverless function (#25 phase 1).

Nothing showed up in LangSmith: the SDK sends runs from a background thread, and
Vercel freezes the function as soon as the request ends. The traces are now sent
before the request finishes, with a time limit, only when tracing is on.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import tracing


@pytest.fixture
def tracing_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LANGSMITH_TRACING", True)
    monkeypatch.setattr(settings, "LANGSMITH_API_KEY", "lsv2-test-key")


def test_traces_are_sent_before_the_request_ends(tracing_on: None) -> None:
    with patch.object(tracing, "_flush_sync") as flush:
        assert TestClient(app).get("/health").status_code == 200

    flush.assert_called_once()


def test_nothing_is_sent_when_tracing_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LANGSMITH_TRACING", False)
    monkeypatch.setattr(settings, "LANGCHAIN_TRACING_V2", False)

    with patch.object(tracing, "_flush_sync") as flush:
        TestClient(app).get("/health")

    flush.assert_not_called()


def test_a_failing_flush_never_breaks_the_answer(tracing_on: None) -> None:
    with patch.object(tracing, "_flush_sync", side_effect=RuntimeError("langsmith down")):
        assert TestClient(app).get("/health").status_code == 200


def test_the_flush_waits_for_langchain_and_the_sdk(tracing_on: None) -> None:
    client = MagicMock()
    with (
        patch("langchain_core.tracers.langchain.wait_for_all_tracers") as wait,
        patch("langsmith.run_trees.get_cached_client", return_value=client),
    ):
        tracing._flush_sync()

    wait.assert_called_once()
    client.flush.assert_called_once()


def test_the_status_line_never_has_the_key(tracing_on: None, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        tracing.log_tracing_status()

    assert "lsv2-test-key" not in caplog.text
    assert "LangSmith" in caplog.text
