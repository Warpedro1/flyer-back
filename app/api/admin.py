"""Admin-only operations (protected by a shared secret, not user JWTs)."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import DbServiceDep
from app.core.config import settings
from app.core.limiter import limiter
from app.services.etl.pipeline import run_ingestion

router = APIRouter(prefix="/admin", tags=["admin"])


class IngestRequest(BaseModel):
    """Trigger payload for a manual ingestion run."""

    model_config = ConfigDict(extra="forbid")

    location: str = Field(min_length=1, max_length=200)
    days: int = Field(default=7, ge=1, le=31)


def _require_admin(token: str | None) -> None:
    """Reject unless the caller presents the configured admin secret (constant-time)."""
    configured = settings.ADMIN_INGEST_TOKEN
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ingestion is disabled (ADMIN_INGEST_TOKEN not set).",
        )
    if not token or not hmac.compare_digest(token, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin token.",
        )


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("2/minute")
async def trigger_ingest(
    request: Request,
    body: IngestRequest,
    db: DbServiceDep,
    background: BackgroundTasks,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict[str, object]:
    """Kick off extract → transform → load in the background; returns 202 immediately.

    The heavy, costly work (scrape + LLM + embeddings) runs after the response so the
    caller isn't held open; results are logged. Global write → service_role via db.
    """
    _require_admin(x_admin_token)
    background.add_task(run_ingestion, db, body.location, body.days)
    return {"status": "accepted", "location": body.location, "days": body.days}
