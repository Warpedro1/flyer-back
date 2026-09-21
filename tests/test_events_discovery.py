"""Discovery-side helpers: past-event filtering and lazy interest-vector backfill."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.events import filter_future_events
from app.services.onboarding_pipeline import ensure_interest_embedding


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_filter_future_events_drops_past_keeps_future_and_undated() -> None:
    now = datetime.now(timezone.utc)
    past = {"id": "past", "event_date": _iso(now - timedelta(days=1))}
    future = {"id": "future", "event_date": _iso(now + timedelta(days=1))}
    undated = {"id": "undated", "event_date": None}

    kept = filter_future_events([past, future, undated])

    assert [r["id"] for r in kept] == ["future", "undated"]


def test_filter_future_events_preserves_input_order() -> None:
    now = datetime.now(timezone.utc)
    rows = [
        {"id": "a", "event_date": _iso(now + timedelta(hours=3))},
        {"id": "b", "event_date": _iso(now + timedelta(hours=1))},
        {"id": "c", "event_date": _iso(now + timedelta(hours=2))},
    ]

    kept = filter_future_events(rows)

    # Ranking order from the RPC must survive the filter (not re-sorted by date).
    assert [r["id"] for r in kept] == ["a", "b", "c"]


def test_filter_future_events_treats_naive_datetimes_as_utc() -> None:
    now = datetime.now(timezone.utc)
    past_naive = {"id": "p", "event_date": (now - timedelta(days=2)).replace(tzinfo=None).isoformat()}
    future_naive = {"id": "f", "event_date": (now + timedelta(days=2)).replace(tzinfo=None).isoformat()}

    kept = filter_future_events([past_naive, future_naive])

    assert [r["id"] for r in kept] == ["f"]


@pytest.mark.asyncio
async def test_ensure_interest_embedding_rebuilds_from_preferences_text() -> None:
    db = MagicMock()
    db.update_profile_embedding = AsyncMock()
    profile = {"id": "u1", "onboarding_preferences_text": "gosto de shows de jazz e trilhas"}
    vec = [0.1] * 1536

    with patch(
        "app.services.onboarding_pipeline.ai_service.generate_embedding",
        new_callable=AsyncMock,
        return_value=vec,
    ):
        out = await ensure_interest_embedding(db, "u1", "jwt", profile)

    assert out == vec
    db.update_profile_embedding.assert_awaited_once_with("u1", vec)


@pytest.mark.asyncio
async def test_ensure_interest_embedding_returns_none_without_preferences_text() -> None:
    db = MagicMock()
    db.update_profile_embedding = AsyncMock()

    out = await ensure_interest_embedding(db, "u1", "jwt", {"id": "u1"})

    assert out is None
    db.update_profile_embedding.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_interest_embedding_swallows_embedding_errors() -> None:
    db = MagicMock()
    db.update_profile_embedding = AsyncMock()
    profile = {"id": "u1", "onboarding_preferences_text": "algum texto"}

    with patch(
        "app.services.onboarding_pipeline.ai_service.generate_embedding",
        new_callable=AsyncMock,
        side_effect=RuntimeError("openai down"),
    ):
        out = await ensure_interest_embedding(db, "u1", "jwt", profile)

    # Best-effort: a provider failure must not persist anything nor raise.
    assert out is None
    db.update_profile_embedding.assert_not_awaited()
