"""Contracts for the ETL ingestion pipeline."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

# Raw provider payloads vary wildly per source, so they stay as plain dicts.
RawEvent = dict[str, object]


class StandardizedEvent(BaseModel):
    """LLM-normalized event, validated before it touches the DB (anti-garbage-in)."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=50)
    source_event_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=100)
    location_name: str | None = Field(default=None, max_length=200)
    lat: float = Field(ge=-90.0, le=90.0)
    long: float = Field(ge=-180.0, le=180.0)
    event_date: datetime | None = None
    price: Decimal | None = Field(default=None, ge=0)


class IngestionReport(BaseModel):
    """Summary of one ingestion run (returned by manual trigger / logged by cron)."""

    model_config = ConfigDict(extra="forbid")

    source: str = ""
    fetched: int = 0
    standardized: int = 0
    upserted: int = 0
    errors: list[str] = Field(default_factory=list)
