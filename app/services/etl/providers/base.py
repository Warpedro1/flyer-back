"""Provider abstraction so the extract stage is decoupled from Apify/SerpApi/etc."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.services.etl.schemas import RawEvent


class EventProvider(ABC):
    """Fetch raw event listings for a location. Implementations own their transport."""

    #: Short, stable identifier stored on each event row (used as the upsert key prefix).
    source: str = "unknown"

    @abstractmethod
    async def fetch(self, location: str, days: int) -> list[RawEvent]:
        """Return raw (un-standardized) event dicts for the next ``days`` at ``location``."""
        raise NotImplementedError
