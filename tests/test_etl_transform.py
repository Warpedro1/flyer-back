"""ETL transform: validates LLM output and drops unknown/invalid rows."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.etl.transform import standardize_events


@pytest.mark.asyncio
async def test_standardize_keeps_valid_drops_unknown_and_invalid() -> None:
    raw = [
        {"id": "a1", "title": "Show bruto", "latitude": -8.0, "longitude": -34.0},
        {"id": "a2", "title": "Outro", "latitude": -8.1, "longitude": -34.1},
    ]
    llm_out = {
        "events": [
            {  # valid, maps to a1
                "source_event_id": "a1",
                "title": "Show de Frevo",
                "description": "Uma noite animada.",
                "category": "music",
                "location_name": "Recife",
                "lat": -8.0,
                "long": -34.0,
                "price": "40.00",
                "extra_field_should_be_ignored": "x",
            },
            {  # hallucinated id → dropped
                "source_event_id": "ghost",
                "title": "Fantasma",
                "lat": 0.0,
                "long": 0.0,
            },
            {  # known id but invalid coords → dropped
                "source_event_id": "a2",
                "title": "Inválido",
                "lat": 999.0,
                "long": -34.1,
            },
        ]
    }

    with patch(
        "app.services.etl.transform.ai_service.json_completion",
        new_callable=AsyncMock,
        return_value=json.dumps(llm_out),
    ):
        out = await standardize_events(raw, source="apify", model="gpt-4o-mini")

    assert len(out) == 1
    ev = out[0]
    assert ev.source == "apify"
    assert ev.source_event_id == "a1"
    assert ev.title == "Show de Frevo"
    assert str(ev.price) == "40.00"


@pytest.mark.asyncio
async def test_standardize_empty_input_makes_no_llm_call() -> None:
    with patch(
        "app.services.etl.transform.ai_service.json_completion",
        new_callable=AsyncMock,
    ) as llm:
        out = await standardize_events([], source="apify")

    assert out == []
    llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_standardize_returns_empty_on_bad_json() -> None:
    raw = [{"id": "a1", "title": "X", "latitude": -8.0, "longitude": -34.0}]
    with patch(
        "app.services.etl.transform.ai_service.json_completion",
        new_callable=AsyncMock,
        return_value="not json at all",
    ):
        out = await standardize_events(raw, source="apify")

    assert out == []
