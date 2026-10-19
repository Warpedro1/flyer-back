"""Admin-only operations (protected by shared secrets, not user JWTs).

Ingestion runs in two steps because scraping takes minutes and a Vercel function at
most 60 s: POST /admin/ingest starts the Apify run and returns; when the run ends,
Apify calls POST /admin/ingest/apify-webhook, which loads that run's dataset.

POST /admin/events/keywords gives the stale events and places keywords and a new
vector (#22 B), a chunk per call; by default it only estimates the cost.

POST /admin/events/moderation checks, a chunk per call, the rows the moderation has
not seen (#22 A): the catalogue that was here before it, and what an import left
unchecked. The moderation is free, so there is no estimate; ``dry_run`` only counts.
"""

from __future__ import annotations

import hmac
import logging
from decimal import Decimal
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import DbServiceDep, HttpClientDep
from app.core.config import settings
from app.core.errors import ApiError
from app.core.limiter import limiter
from app.services.etl.apify import monthly_usage, start_run
from app.services.etl.pipeline import ingest_dataset
from app.services.etl.schemas import IngestionReport
from app.services.etl.sources import SOURCES
from app.services.event_keywords import all_stale, estimate_cost, reprocess_stale
from app.services.moderation import moderate_stale

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

SourceParam = Literal["google_maps", "sympla"]


class IngestRequest(BaseModel):
    """Which source to scrape now."""

    model_config = ConfigDict(extra="forbid")

    source: SourceParam


class ApifyRun(BaseModel):
    """The part of the run object Apify sends that the webhook uses."""

    # Apify's payload has many more fields, and adds new ones; only these matter here.
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    status: str | None = None
    defaultDatasetId: str = Field(min_length=1, max_length=100)


class ApifyWebhook(BaseModel):
    """Apify's default webhook payload."""

    model_config = ConfigDict(extra="ignore")

    eventType: str | None = None
    resource: ApifyRun


class KeywordsRequest(BaseModel):
    """One call of the keyword reprocessing."""

    model_config = ConfigDict(extra="forbid")

    # Decision 2026-10-18: the owner sees the cost before anything is spent, so the
    # default is the estimate alone.
    dry_run: bool = True
    # Rows tried in this call; what the time budget leaves undone stays stale. At
    # most one embedding batch, so the call ends inside the 60 s of a function.
    limit: int = Field(default=80, ge=1, le=100)


class KeywordsEstimate(BaseModel):
    """Rough cost of the rows still stale (list prices, about 4 characters per token)."""

    model_config = ConfigDict(extra="forbid")

    items: int
    input_tokens: int
    output_tokens: int
    embedding_tokens: int
    usd: Decimal


class KeywordsReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool
    # Stale rows before this call, and after it.
    pending: int
    processed: int = 0
    failed: int = 0
    remaining: int
    estimate: KeywordsEstimate


class ModerationRequest(BaseModel):
    """One call of the moderation pass."""

    model_config = ConfigDict(extra="forbid")

    dry_run: bool = False
    # Imported rows: one image call per cover, 8 at a time. App rows (photos and
    # icon, 4 rows at a time) may not all fit: what does not finish inside the budget
    # is left stale for the next call, the rest is written.
    limit: int = Field(default=100, ge=1, le=100)


class ModerationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool
    # Rows still unchecked before this call, and after it.
    pending: int
    checked: int = 0
    blocked: int = 0
    covers_removed: int = 0
    contacts_removed: int = 0
    not_checked: int = 0
    remaining: int


def _require_secret(configured: str, given: str | None, name: str) -> None:
    """Reject unless the caller presents the configured secret (constant-time)."""
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Ingestion is disabled ({name} not set).",
        )
    if not given or not hmac.compare_digest(given, configured):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")


def _require_admin(token: str | None) -> None:
    _require_secret(settings.ADMIN_INGEST_TOKEN, token, "ADMIN_INGEST_TOKEN")


def _require_apify_token() -> str:
    if not settings.APIFY_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ingestion is disabled (APIFY_TOKEN not set).",
        )
    return settings.APIFY_TOKEN


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("2/minute")
async def trigger_ingest(
    request: Request,
    body: IngestRequest,
    http: HttpClientDep,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, str]:
    """Start a capped Apify run for one source; its webhook loads the results."""
    _require_admin(x_admin_token)
    token = _require_apify_token()
    if not settings.APIFY_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ingestion is disabled (APIFY_WEBHOOK_SECRET not set).",
        )
    source = SOURCES[body.source]
    # Run while there are credits (decision 2026-10-19): never start a run whose
    # charge cap does not fit what is left of the month.
    try:
        used, account_limit = await monthly_usage(http, token)
    except Exception as exc:
        # The status says whether it is Apify being down or the token being refused.
        status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        logger.warning("Apify usage unavailable (%s %s)", type(exc).__name__, status_code)
        raise ApiError(
            503, "apify_usage_unavailable", "Não conseguimos consultar o gasto do mês no Apify. Tente de novo mais tarde."
        ) from None
    budget = settings.APIFY_MONTHLY_BUDGET_USD
    if account_limit is not None:
        budget = min(budget, account_limit)
    if used + source.max_charge_usd > budget:
        raise ApiError(
            409,
            "apify_budget_exhausted",
            f"Sem créditos do Apify para esta importação neste mês: gasto US$ {used:.2f} de US$ {budget:.2f},"
            f" e ela pode custar até US$ {source.max_charge_usd:.2f}.",
        )
    base = (settings.PUBLIC_API_BASE_URL or str(request.base_url)).rstrip("/")
    webhook_url = f"{base}/admin/ingest/apify-webhook?source={body.source}"
    run = await start_run(http, token, source, webhook_url, settings.APIFY_WEBHOOK_SECRET)
    return {
        "status": "started",
        "source": body.source,
        "run_id": str(run.get("id", "")),
        "monthly_usage_usd": f"{used:.2f}",
        "monthly_budget_usd": f"{budget:.2f}",
    }


@router.post("/ingest/apify-webhook", response_model=IngestionReport)
@limiter.limit("30/minute")
async def apify_webhook(
    request: Request,
    body: ApifyWebhook,
    db: DbServiceDep,
    http: HttpClientDep,
    source: Annotated[SourceParam, Query()],
    x_apify_webhook_secret: str | None = Header(default=None, alias="X-Apify-Webhook-Secret"),
) -> IngestionReport:
    """Load the dataset of a finished run. Idempotent: Apify's retries re-upsert the same rows."""
    _require_secret(settings.APIFY_WEBHOOK_SECRET, x_apify_webhook_secret, "APIFY_WEBHOOK_SECRET")
    token = _require_apify_token()
    return await ingest_dataset(db, http, SOURCES[source], body.resource.defaultDatasetId, token)


@router.post("/events/keywords", response_model=KeywordsReport)
@limiter.limit("10/minute")
async def reprocess_keywords(
    request: Request,
    db: DbServiceDep,
    body: KeywordsRequest | None = None,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> KeywordsReport:
    """Estimate (``dry_run``, the default) or give keywords to the next stale rows.

    Call it again until ``remaining`` is 0; each call fits a 60 s function.
    """
    _require_admin(x_admin_token)
    req = body or KeywordsRequest()
    stale = await all_stale(db)
    cost = estimate_cost(stale)
    estimate = KeywordsEstimate(
        items=cost.items,
        input_tokens=cost.input_tokens,
        output_tokens=cost.output_tokens,
        embedding_tokens=cost.embedding_tokens,
        usd=cost.usd,
    )
    if req.dry_run:
        return KeywordsReport(dry_run=True, pending=len(stale), remaining=len(stale), estimate=estimate)
    done = await reprocess_stale(db, limit=req.limit, budget_seconds=settings.KEYWORDS_BUDGET_SECONDS)
    return KeywordsReport(
        dry_run=False,
        pending=len(stale),
        processed=done.processed,
        failed=done.failed,
        remaining=max(0, len(stale) - done.processed),
        estimate=estimate,
    )


async def _count_unmoderated(db: DbServiceDep) -> int:
    total, page = 0, 1000
    while True:
        rows = await db.list_stale_moderation_events(limit=page, offset=total)
        total += len(rows)
        if len(rows) < page:
            return total


@router.post("/events/moderation", response_model=ModerationReport)
@limiter.limit("10/minute")
async def moderate_catalogue(
    request: Request,
    db: DbServiceDep,
    body: ModerationRequest | None = None,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> ModerationReport:
    """Check the next unchecked rows (or, with ``dry_run``, only count them).

    Call it again until ``remaining`` is 0; each call fits a 60 s function.
    """
    _require_admin(x_admin_token)
    req = body or ModerationRequest()
    pending = await _count_unmoderated(db)
    if req.dry_run:
        return ModerationReport(dry_run=True, pending=pending, remaining=pending)
    done = await moderate_stale(db, limit=req.limit, budget_seconds=settings.KEYWORDS_BUDGET_SECONDS)
    return ModerationReport(
        dry_run=False,
        pending=pending,
        checked=done.checked,
        blocked=done.blocked,
        covers_removed=done.covers_removed,
        contacts_removed=done.contacts_removed,
        not_checked=done.not_checked,
        remaining=max(0, pending - done.checked),
    )
