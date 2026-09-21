"""Orchestrate extract → transform → load and report the outcome."""

from __future__ import annotations

import logging

from app.core.config import settings
from app.services.db_service import DBService
from app.services.etl.load import upsert_events
from app.services.etl.providers.apify_provider import ApifyProvider
from app.services.etl.providers.base import EventProvider
from app.services.etl.providers.mock_provider import MockEventProvider
from app.services.etl.schemas import IngestionReport
from app.services.etl.transform import standardize_events

logger = logging.getLogger(__name__)


def build_default_provider() -> EventProvider:
    """Apify when a token is configured; otherwise a mock provider for dev/CI."""
    if settings.APIFY_TOKEN:
        return ApifyProvider(
            settings.APIFY_TOKEN,
            settings.APIFY_EVENTS_ACTOR,
            lang=settings.APIFY_EVENTS_LANG,
            country=settings.APIFY_EVENTS_COUNTRY,
            canonical_location=settings.APIFY_EVENTS_LOCATION,
        )
    logger.warning("APIFY_TOKEN not set — ingestion will use MockEventProvider.")
    return MockEventProvider()


async def run_ingestion(
    db: DBService,
    location: str,
    days: int,
    provider: EventProvider | None = None,
) -> IngestionReport:
    """Run one ingestion pass. Never raises: failures are captured in the report."""
    provider = provider or build_default_provider()
    report = IngestionReport(source=provider.source)

    try:
        raw = await provider.fetch(location, days)
    except Exception as exc:  # noqa: BLE001 (report, don't crash the job)
        logger.exception("ETL extract failed (source=%s)", provider.source)
        report.errors.append(f"extract: {exc}")
        return report
    report.fetched = len(raw)

    events = await standardize_events(raw, provider.source)
    report.standardized = len(events)

    try:
        report.upserted = await upsert_events(db, events)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ETL load failed (source=%s)", provider.source)
        report.errors.append(f"load: {exc}")

    logger.info(
        "ETL run done: source=%s fetched=%d standardized=%d upserted=%d errors=%d",
        report.source, report.fetched, report.standardized, report.upserted, len(report.errors),
    )
    return report
