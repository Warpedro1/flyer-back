"""Apify provider: builds the actor's input and flattens its wrapped dataset item."""

from __future__ import annotations

import httpx
import pytest

from app.services.etl.providers.apify_provider import ApifyProvider

_DATASET_ITEM = {
    "search_parameters": {"q": "eventos em Recife", "location": "Recife"},
    "search_metadata": {"events_count": 2},
    "events": [
        {"title": "Show de Frevo", "type": "Live concert", "address": ["Marco Zero"]},
        {"title": "Feira de Rua", "type": "Festival", "address": ["Boa Viagem"],
         "location": "Boa Viagem, Recife"},
    ],
}


@pytest.mark.asyncio
async def test_fetch_flattens_events_and_sends_actor_input() -> None:
    captured: dict[str, object] = {}

    provider = ApifyProvider("tok", "acme~actor", lang="pt-BR", country="BR")

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, params: dict, json: dict) -> httpx.Response:
            captured["url"] = url
            captured["params"] = params
            captured["json"] = json
            return httpx.Response(
                200,
                json=[_DATASET_ITEM],
                request=httpx.Request("POST", url),
            )

    import app.services.etl.providers.apify_provider as mod

    original = mod.httpx.AsyncClient
    mod.httpx.AsyncClient = _Client  # type: ignore[assignment]
    try:
        out = await provider.fetch("Recife", days=7)
    finally:
        mod.httpx.AsyncClient = original  # type: ignore[assignment]

    body = captured["json"]
    assert body["q"] == "eventos em Recife"
    assert body["advanced"] == "date:week"
    # regional tags and casing are normalized; the actor rejects "pt-BR"
    assert body["hl"] == "pt" and body["gl"] == "br"
    # a free-text city must NOT go in the actor's `location` field (it aborts the run)
    assert "location" not in body
    assert captured["params"] == {"token": "tok"}

    assert len(out) == 2
    assert out[0]["title"] == "Show de Frevo"
    # listings carry no coordinates, so the searched location rides along for the LLM
    assert out[0]["location"] == "Recife"
    # an event that already names its own location keeps it
    assert out[1]["location"] == "Boa Viagem, Recife"


@pytest.mark.parametrize(
    ("days", "token"),
    [(1, "date:today"), (7, "date:week"), (30, "date:month")],
)
def test_date_token_windows(days: int, token: str) -> None:
    from app.services.etl.providers.apify_provider import _date_token

    assert _date_token(days) == token


def test_flatten_accepts_a_plain_list_of_events() -> None:
    """A provider/actor that returns one row per event still works."""
    out = ApifyProvider._flatten([{"title": "A"}, {"title": "B"}])
    assert [e["title"] for e in out] == ["A", "B"]


def test_canonical_location_is_sent_when_configured() -> None:
    provider = ApifyProvider("tok", "acme~actor", canonical_location="Lisbon,Lisbon,Portugal")
    body = provider._run_input("Lisboa", days=3)
    assert body["location"] == "Lisbon,Lisbon,Portugal"
    assert body["q"] == "eventos em Lisboa"


def test_flatten_raises_on_actor_error_row() -> None:
    """The actor reports bad input as a dataset row, so the report must show the reason."""
    with pytest.raises(RuntimeError, match="Unsupported"):
        ApifyProvider._flatten(
            [{"error": True, "error_message": "Unsupported `Lisboa` location - location parameter."}]
        )
