"""Contracts for the ETL ingestion pipeline."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.schemas import EventCategory, EventKind, OpeningHours

# Raw source payloads vary wildly per source, so they stay as plain dicts.
RawEvent = dict[str, object]


class StandardizedEvent(BaseModel):
    """An item ready for `events`, validated before it touches the DB (anti-garbage-in)."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=50)
    source_event_id: str = Field(min_length=1, max_length=200)
    kind: EventKind = "event"
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    category: EventCategory = "outros"
    location_name: str | None = Field(default=None, max_length=200)
    # Always from the source: an item without coordinates is dropped, never guessed.
    lat: float = Field(ge=-90.0, le=90.0)
    long: float = Field(ge=-180.0, le=180.0)
    event_date: datetime | None = None
    # Events: check-in opens at the start and closes at the end (decision 2026-10-12).
    ends_at: datetime | None = None
    checkin_opens_at: datetime | None = None
    # Events: exact price in BRL (0 = free, None = unknown). Places: price_level.
    price: Decimal | None = Field(default=None, ge=0)
    price_level: int | None = Field(default=None, ge=1, le=4)
    rating: Decimal | None = Field(default=None, ge=0, le=5)
    opening_hours: list[OpeningHours] | None = None
    source_url: str | None = Field(default=None, max_length=2000)
    cover_url: str | None = Field(default=None, max_length=2000)


class IngestionReport(BaseModel):
    """Summary of one ingestion run (returned by the webhook and logged)."""

    model_config = ConfigDict(extra="forbid")

    source: str = ""
    fetched: int = 0
    standardized: int = 0
    upserted: int = 0
    linked: int = 0
    # Rows given keywords and a new vector in this run (#22 B).
    keywords: int = 0
    # Moderation (#22 A): refused (never entered, or hidden), not checked in time,
    # covers dropped, and items whose contacts became "[contato removido]".
    blocked: int = 0
    moderation_pending: int = 0
    covers_removed: int = 0
    contacts_removed: int = 0
    errors: list[str] = Field(default_factory=list)
