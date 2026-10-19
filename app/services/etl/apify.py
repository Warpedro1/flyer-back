"""The Apify API calls the ingestion needs, over the app's shared httpx client."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from app.services.etl.sources import ApifySource

APIFY_BASE = "https://api.apify.com/v2"
DATASET_PAGE = 1000
WEBHOOK_EVENTS = ("ACTOR.RUN.SUCCEEDED", "ACTOR.RUN.TIMED_OUT", "ACTOR.RUN.ABORTED")


def _auth(token: str) -> dict[str, str]:
    # Header, not ?token=: URLs end up in logs.
    return {"Authorization": f"Bearer {token}"}


async def start_run(
    http: httpx.AsyncClient,
    token: str,
    source: ApifySource,
    webhook_url: str,
    webhook_secret: str,
) -> dict[str, Any]:
    """Start the actor without waiting; Apify calls `webhook_url` when the run ends.

    Timed-out or aborted runs (for example, the charge cap reached) still call back:
    their partial dataset is worth loading.
    """
    webhooks = [
        {
            "eventTypes": list(WEBHOOK_EVENTS),
            "requestUrl": webhook_url,
            "headersTemplate": json.dumps({"X-Apify-Webhook-Secret": webhook_secret}),
        }
    ]
    r = await http.post(
        f"{APIFY_BASE}/acts/{source.actor}/runs",
        params={
            "maxTotalChargeUsd": str(source.max_charge_usd),
            "webhooks": base64.b64encode(json.dumps(webhooks).encode()).decode(),
        },
        json=source.run_input(),
        headers=_auth(token),
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json().get("data")
    return data if isinstance(data, dict) else {}


async def monthly_usage(http: httpx.AsyncClient, token: str) -> tuple[float, float | None]:
    """What the account has spent in the current monthly cycle (USD), and its own
    monthly limit when it has one."""
    r = await http.get(f"{APIFY_BASE}/users/me/limits", headers=_auth(token), timeout=15.0)
    r.raise_for_status()
    data = r.json().get("data") or {}
    used = float((data.get("current") or {}).get("monthlyUsageUsd") or 0.0)
    limit = (data.get("limits") or {}).get("maxMonthlyUsageUsd")
    return used, float(limit) if limit is not None else None


async def fetch_dataset_items(http: httpx.AsyncClient, token: str, dataset_id: str) -> list[dict[str, Any]]:
    """Every item of a run's dataset, page by page."""
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        r = await http.get(
            f"{APIFY_BASE}/datasets/{dataset_id}/items",
            params={"clean": "true", "format": "json", "limit": DATASET_PAGE, "offset": offset},
            headers=_auth(token),
            timeout=30.0,
        )
        r.raise_for_status()
        page = r.json()
        if not isinstance(page, list):
            break
        items.extend(i for i in page if isinstance(i, dict))
        if len(page) < DATASET_PAGE:
            break
        offset += DATASET_PAGE
    return items
