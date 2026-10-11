"""Admin-only operations (protected by shared secrets, not user JWTs).

Ingestion runs in two steps because scraping takes minutes and a Vercel function at
most 60 s: POST /admin/ingest starts the Apify run and returns; when the run ends,
Apify calls POST /admin/ingest/apify-webhook, which loads that run's dataset.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import DbServiceDep, HttpClientDep
from app.core.config import settings
from app.core.limiter import limiter
from app.services.etl.apify import start_run
from app.services.etl.pipeline import ingest_dataset
from app.services.etl.schemas import IngestionReport
from app.services.etl.sources import SOURCES

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
    base = (settings.PUBLIC_API_BASE_URL or str(request.base_url)).rstrip("/")
    webhook_url = f"{base}/admin/ingest/apify-webhook?source={body.source}"
    run = await start_run(http, token, SOURCES[body.source], webhook_url, settings.APIFY_WEBHOOK_SECRET)
    return {"status": "started", "source": body.source, "run_id": str(run.get("id", ""))}


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
