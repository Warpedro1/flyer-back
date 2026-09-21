"""In-memory provider for local dev / tests (no external calls, no token required)."""

from __future__ import annotations

from app.services.etl.providers.base import EventProvider
from app.services.etl.schemas import RawEvent


class MockEventProvider(EventProvider):
    """Returns a fixed (or injected) set of raw events."""

    source = "mock"

    def __init__(self, items: list[RawEvent] | None = None) -> None:
        self._items = items if items is not None else _SAMPLE

    async def fetch(self, location: str, days: int) -> list[RawEvent]:  # noqa: ARG002
        return list(self._items)


_SAMPLE: list[RawEvent] = [
    {
        "id": "mock-1",
        "title": "Feira Gastronômica no Marco Zero",
        "description": "Comidas típicas e food trucks à beira do Recife Antigo.",
        "category": "food",
        "location": "Marco Zero, Recife",
        "latitude": -8.0631,
        "longitude": -34.8711,
        "startDate": "2026-07-25T18:00:00",
        "price": "0",
    },
    {
        "id": "mock-2",
        "title": "Show de Frevo e Maracatu",
        "description": "Apresentação ao vivo no coração do bairro do Recife.",
        "category": "music",
        "location": "Cais do Sertão, Recife",
        "latitude": -8.0619,
        "longitude": -34.8719,
        "startDate": "2026-07-26T20:00:00",
        "price": "40.00",
    },
]
