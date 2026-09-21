"""ETL pipeline orchestration + admin trigger guard."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.admin import _require_admin
from app.core.config import settings
from app.services.etl.pipeline import run_ingestion
from app.services.etl.providers.base import EventProvider
from app.services.etl.providers.mock_provider import MockEventProvider


@pytest.mark.asyncio
async def test_run_ingestion_reports_counts() -> None:
    provider = MockEventProvider(items=[{"id": "1"}, {"id": "2"}])
    db = MagicMock()

    with (
        patch(
            "app.services.etl.pipeline.standardize_events",
            new_callable=AsyncMock,
            return_value=["e1", "e2"],
        ),
        patch(
            "app.services.etl.pipeline.upsert_events",
            new_callable=AsyncMock,
            return_value=2,
        ),
    ):
        report = await run_ingestion(db, "Recife", 7, provider=provider)

    assert report.source == "mock"
    assert report.fetched == 2
    assert report.standardized == 2
    assert report.upserted == 2
    assert report.errors == []


@pytest.mark.asyncio
async def test_run_ingestion_captures_extract_failure() -> None:
    class BoomProvider(EventProvider):
        source = "boom"

        async def fetch(self, location: str, days: int) -> list[dict[str, object]]:
            raise RuntimeError("network down")

    report = await run_ingestion(MagicMock(), "Recife", 7, provider=BoomProvider())

    assert report.fetched == 0
    assert report.upserted == 0
    assert any("extract:" in e for e in report.errors)


def test_require_admin_disabled_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ADMIN_INGEST_TOKEN", "")
    with pytest.raises(HTTPException) as ei:
        _require_admin("whatever")
    assert ei.value.status_code == 503


def test_require_admin_wrong_token_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ADMIN_INGEST_TOKEN", "secret")
    with pytest.raises(HTTPException) as ei:
        _require_admin("nope")
    assert ei.value.status_code == 401


def test_require_admin_accepts_correct_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ADMIN_INGEST_TOKEN", "secret")
    _require_admin("secret")  # must not raise
