"""Load one finished Apify run: extract its dataset → transform → load → link → keywords."""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.core.config import settings
from app.services.db_service import DBService
from app.services.etl.apify import fetch_dataset_items
from app.services.etl.load import link_events_to_places, upsert_events
from app.services.etl.schemas import IngestionReport, StandardizedEvent
from app.services.etl.sources import ApifySource
from app.services.event_keywords import EMBED_BATCH, reprocess_stale
from app.services.moderation import redact_import

logger = logging.getLogger(__name__)

# The webhook runs in a 60 s function: the LLM calls stop at this mark, which
# leaves one embedding batch (`EMBEDDING_TIMEOUT_SECONDS`), the error record and
# the writes inside the limit. Hence at most one batch of rows per run.
_WEBHOOK_SECONDS = 42.0
_MIN_KEYWORDS_SECONDS = 5.0
_KEYWORDS_PER_RUN = EMBED_BATCH


async def ingest_dataset(
    db: DBService,
    http: httpx.AsyncClient,
    source: ApifySource,
    dataset_id: str,
    token: str,
) -> IngestionReport:
    """Never raises: failures are captured in the report (and logged)."""
    report = IngestionReport(source=source.name)
    loop = asyncio.get_running_loop()
    started = loop.time()

    try:
        raw = await fetch_dataset_items(http, token, dataset_id)
    except Exception as exc:  # report, don't crash the webhook
        logger.exception("ETL extract failed (source=%s, dataset=%s)", source.name, dataset_id)
        report.errors.append(f"extract: {exc}")
        return report
    report.fetched = len(raw)

    events: list[StandardizedEvent] = []
    seen: set[str] = set()
    left_out: set[str] = set()
    for item in raw:
        ev = source.transform(item)
        # The same place can come from two searches; one row per source id.
        if ev is None or ev.source_event_id in seen:
            continue
        # Only what pleases most people (decision 2026-10-19). Another search may
        # still bring the same place in, with a lower bar.
        if not source.keep(item, ev):
            left_out.add(ev.source_event_id)
            continue
        seen.add(ev.source_event_id)
        events.append(ev)
    report.standardized = len(events)
    report.not_popular = len(left_out - seen)
    # Most popular first: the moderation and keyword budgets reach those first.
    events.sort(key=source.rank)

    # LGPD (#22 A): contacts in the texts never enter, before anything is hashed.
    for i, ev in enumerate(events):
        title, description, changed = redact_import(ev.title, ev.description)
        if changed:
            events[i] = ev.model_copy(update={"title": title, "description": description})
            report.contacts_removed += 1

    try:
        loaded = await upsert_events(db, events)
        report.upserted = loaded.upserted
        report.blocked = loaded.blocked
        report.moderation_pending = loaded.pending
        report.covers_removed = loaded.covers_removed
        report.linked = await link_events_to_places(db, events)
    except Exception as exc:
        logger.exception("ETL load failed (source=%s, dataset=%s)", source.name, dataset_id)
        report.errors.append(f"load: {exc}")

    # Keywords for the stale rows (#22 B) with the time left, once the owner has
    # approved the backlog's cost (KEYWORDS_ON_IMPORT, decision 2026-10-18); the
    # reprocessing (POST /admin/events/keywords) does the rest.
    budget = min(settings.KEYWORDS_BUDGET_SECONDS, _WEBHOOK_SECONDS - (loop.time() - started))
    if settings.KEYWORDS_ON_IMPORT and budget >= _MIN_KEYWORDS_SECONDS:
        try:
            done = await reprocess_stale(db, limit=_KEYWORDS_PER_RUN, budget_seconds=budget)
            report.keywords = done.processed
        except Exception as exc:
            logger.exception("ETL keywords failed (source=%s)", source.name)
            report.errors.append(f"keywords: {exc}")

    logger.info(
        "ETL run done: source=%s fetched=%d standardized=%d not_popular=%d upserted=%d linked=%d "
        "keywords=%d blocked=%d moderation_pending=%d covers_removed=%d contacts_removed=%d errors=%d",
        report.source, report.fetched, report.standardized, report.not_popular, report.upserted,
        report.linked, report.keywords, report.blocked, report.moderation_pending,
        report.covers_removed, report.contacts_removed, len(report.errors),
    )
    return report
