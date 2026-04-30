"""Public plans/pricing endpoints."""

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request

from app.api.deps import DbServiceDep
from app.core.limiter import limiter
from app.models.schemas import PlanRead

router = APIRouter(prefix="/plans", tags=["plans"])


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _row_to_plan(row: dict[str, Any]) -> PlanRead:
    features_raw = row.get("features")
    features: list[str] = []
    if isinstance(features_raw, list):
        features = [str(f) for f in features_raw]
    return PlanRead(
        id=str(row["id"]),
        name=str(row["name"]),
        price=_parse_decimal(row.get("price")),
        interval=row.get("interval"),
        features=features,
        is_active=bool(row.get("is_active", True)),
    )


@router.get("/", response_model=list[PlanRead])
@limiter.limit("30/minute")
async def list_plans(request: Request, db: DbServiceDep) -> list[PlanRead]:
    """Public endpoint — no auth required."""
    rows = await db.get_plans()
    return [_row_to_plan(r) for r in rows]


@router.get("/health")
async def plans_health() -> dict[str, str]:
    return {"status": "ok"}
