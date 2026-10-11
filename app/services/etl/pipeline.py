"""Load one finished Apify run: extract its dataset → transform → load → link."""

from __future__ import annotations

import logging

import httpx

from app.services.db_service import DBService
from app.services.etl.apify import fetch_dataset_items
from app.services.etl.load import link_events_to_places, upsert_events
from app.services.etl.schemas import IngestionReport, StandardizedEvent
from app.services.etl.sources import ApifySource

logger = logging.getLogger(__name__)


async def ingest_dataset(
    db: DBService,
    http: httpx.AsyncClient,
    source: ApifySource,
    dataset_id: str,
    token: str,
) -> IngestionReport:
    """Never raises: failures are captured in the report (and logged)."""
    report = IngestionReport(source=source.name)

    try:
        raw = await fetch_dataset_items(http, token, dataset_id)
    except Exception as exc:  # report, don't crash the webhook
        logger.exception("ETL extract failed (source=%s, dataset=%s)", source.name, dataset_id)
        report.errors.append(f"extract: {exc}")
        return report
    report.fetched = len(raw)

    events: list[StandardizedEvent] = []
    seen: set[str] = set()
    for item in raw:
        ev = source.transform(item)
        # The same place can come from two searches; one row per source id.
        if ev is not None and ev.source_event_id not in seen:
            seen.add(ev.source_event_id)
            events.append(ev)
    report.standardized = len(events)

    try:
        report.upserted = await upsert_events(db, events)
        report.linked = await link_events_to_places(db, events)
    except Exception as exc:
        logger.exception("ETL load failed (source=%s, dataset=%s)", source.name, dataset_id)
        report.errors.append(f"load: {exc}")

    logger.info(
        "ETL run done: source=%s fetched=%d standardized=%d upserted=%d linked=%d errors=%d",
        report.source, report.fetched, report.standardized, report.upserted, report.linked,
        len(report.errors),
    )
    return report
