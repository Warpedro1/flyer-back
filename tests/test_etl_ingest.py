"""Ingestion through Apify: start a run, then load its dataset from the webhook.

The scraping takes minutes, longer than a Vercel function may run, so POST
/admin/ingest only starts the run; Apify calls the webhook when it finishes and the
backend loads that run's dataset (transform + embeddings + upsert, ~600 items).
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_db_service, get_http_client
from app.core.config import settings
from app.core.limiter import limiter
from app.main import app
from app.services.etl.apify import fetch_dataset_items, monthly_usage, start_run
from app.services.etl.load import LoadResult
from app.services.etl.pipeline import ingest_dataset
from app.services.etl.schemas import IngestionReport
from app.services.etl.sources import SOURCES
from app.services.event_keywords import ReprocessResult

ADMIN = "admin-token-for-tests"
HOOK = "webhook-secret-for-tests"


def _response(method: str, payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request(method, "https://api.apify.com"))


# ------------------------------------------------------------- apify api --


async def test_start_run_is_asynchronous_capped_and_calls_back() -> None:
    http = MagicMock()
    http.post = AsyncMock(return_value=_response("POST", {"data": {"id": "run-1", "status": "READY"}}))
    source = SOURCES["sympla"]

    run = await start_run(http, "apify-token", source, "https://api.flyer/admin/ingest/apify-webhook?source=sympla", HOOK)

    assert run["id"] == "run-1"
    kwargs = http.post.await_args.kwargs
    assert http.post.await_args.args[0] == f"https://api.apify.com/v2/acts/{source.actor}/runs"
    # The token goes in a header, never in the URL (URLs end up in logs).
    assert kwargs["headers"]["Authorization"] == "Bearer apify-token"
    assert "token" not in kwargs["params"]
    assert kwargs["params"]["maxTotalChargeUsd"] == str(source.max_charge_usd)
    assert kwargs["json"] == source.run_input()
    hooks = json.loads(base64.b64decode(kwargs["params"]["webhooks"]))
    assert hooks[0]["requestUrl"].endswith("/admin/ingest/apify-webhook?source=sympla")
    assert "ACTOR.RUN.SUCCEEDED" in hooks[0]["eventTypes"]
    assert json.loads(hooks[0]["headersTemplate"]) == {"X-Apify-Webhook-Secret": HOOK}


async def test_dataset_items_are_read_page_by_page() -> None:
    http = MagicMock()
    http.get = AsyncMock(
        side_effect=[
            _response("GET", [{"n": i} for i in range(1000)]),
            _response("GET", [{"n": 1000}]),
        ]
    )

    items = await fetch_dataset_items(http, "apify-token", "ds-1")

    assert len(items) == 1001
    offsets = [c.kwargs["params"]["offset"] for c in http.get.await_args_list]
    assert offsets == [0, 1000]


async def test_the_months_usage_comes_from_the_account_limits() -> None:
    http = MagicMock()
    http.get = AsyncMock(
        return_value=_response(
            "GET",
            {"data": {"limits": {"maxMonthlyUsageUsd": 5}, "current": {"monthlyUsageUsd": 1.234}}},
        )
    )

    used, limit = await monthly_usage(http, "apify-token")

    assert (used, limit) == (1.234, 5.0)
    assert http.get.await_args.args[0] == "https://api.apify.com/v2/users/me/limits"
    assert http.get.await_args.kwargs["headers"]["Authorization"] == "Bearer apify-token"


def test_the_pilot_stays_within_its_budget() -> None:
    # Decision 2026-10-11: pilot of ~3 USD. Apify stops charging at the cap.
    assert sum(s.max_charge_usd for s in SOURCES.values()) <= 3.0
    places = SOURCES["google_maps"].run_input()
    assert len(places["searchStringsArray"]) * places["maxCrawledPlacesPerSearch"] <= 600


# --------------------------------------------------------------- pipeline --


async def test_ingest_dataset_transforms_loads_and_links() -> None:
    http, db = MagicMock(), MagicMock()
    good = {"title": "Parraxaxá", "placeId": "p1", "location": {"lat": -8.1, "lng": -34.9}}
    bad = {"title": "Sem coordenadas", "placeId": "p2"}

    with (
        patch("app.services.etl.pipeline.fetch_dataset_items", AsyncMock(return_value=[good, bad])),
        patch("app.services.etl.pipeline.upsert_events", AsyncMock(return_value=LoadResult(upserted=1))) as upsert,
        patch("app.services.etl.pipeline.link_events_to_places", AsyncMock(return_value=0)) as link,
        patch("app.services.etl.pipeline.reprocess_stale", AsyncMock(return_value=ReprocessResult(0, 0, 0))),
    ):
        report = await ingest_dataset(db, http, SOURCES["google_maps"], "ds-1", "tok")

    assert (report.fetched, report.standardized, report.upserted) == (2, 1, 1)
    assert report.errors == []
    assert [e.source_event_id for e in upsert.await_args.args[1]] == ["p1"]
    link.assert_awaited_once()


async def test_ingest_dataset_reports_instead_of_raising() -> None:
    with patch("app.services.etl.pipeline.fetch_dataset_items", AsyncMock(side_effect=httpx.ConnectError("x"))):
        report = await ingest_dataset(MagicMock(), MagicMock(), SOURCES["sympla"], "ds-1", "tok")

    assert report.upserted == 0
    assert report.errors and report.errors[0].startswith("extract:")


# ----------------------------------------------------------------- routes --


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    monkeypatch.setattr(settings, "ADMIN_INGEST_TOKEN", ADMIN)
    monkeypatch.setattr(settings, "APIFY_WEBHOOK_SECRET", HOOK)
    monkeypatch.setattr(settings, "APIFY_TOKEN", "apify-token")
    monkeypatch.setattr(settings, "PUBLIC_API_BASE_URL", "https://api.flyer")
    # /admin/ingest allows 2 calls a minute; each test starts with a fresh budget.
    limiter.reset()
    http = MagicMock()
    app.dependency_overrides[get_http_client] = lambda: http
    app.dependency_overrides[get_db_service] = lambda: MagicMock()
    yield http
    app.dependency_overrides.clear()


def test_admin_ingest_starts_a_run(configured: MagicMock) -> None:
    run = AsyncMock(return_value={"id": "run-1", "status": "READY"})

    with (
        patch("app.api.admin.start_run", run),
        patch("app.api.admin.monthly_usage", AsyncMock(return_value=(1.0, 5.0))),
    ):
        response = TestClient(app).post(
            "/admin/ingest", json={"source": "google_maps"}, headers={"X-Admin-Token": ADMIN}
        )

    assert response.status_code == 202
    assert response.json() == {
        "status": "started",
        "source": "google_maps",
        "run_id": "run-1",
        "monthly_usage_usd": "1.00",
        "monthly_budget_usd": "5.00",
    }
    args = run.await_args.args
    assert args[2] is SOURCES["google_maps"]
    assert args[3] == "https://api.flyer/admin/ingest/apify-webhook?source=google_maps"
    assert args[4] == HOOK


@pytest.mark.parametrize(
    ("used", "account_limit", "source"),
    [
        # Decision 2026-10-19: 5 USD a month on Apify, run while there are credits.
        (4.5, None, "sympla"),  # 4.5 + 0.6 cap > 5
        (3.0, None, "google_maps"),  # 3.0 + 2.4 cap > 5
        (1.0, 2.0, "google_maps"),  # the account's own limit is lower
    ],
)
def test_admin_ingest_does_not_start_a_run_the_months_credits_cannot_pay(
    configured: MagicMock, used: float, account_limit: float | None, source: str
) -> None:
    run = AsyncMock()

    with (
        patch("app.api.admin.start_run", run),
        patch("app.api.admin.monthly_usage", AsyncMock(return_value=(used, account_limit))),
    ):
        response = TestClient(app).post("/admin/ingest", json={"source": source}, headers={"X-Admin-Token": ADMIN})

    assert response.status_code == 409
    assert response.json()["code"] == "apify_budget_exhausted"
    run.assert_not_awaited()


def test_admin_ingest_does_not_guess_the_usage(configured: MagicMock) -> None:
    run = AsyncMock()

    with (
        patch("app.api.admin.start_run", run),
        patch("app.api.admin.monthly_usage", AsyncMock(side_effect=httpx.ConnectError("down"))),
    ):
        response = TestClient(app).post("/admin/ingest", json={"source": "sympla"}, headers={"X-Admin-Token": ADMIN})

    assert response.status_code == 503
    run.assert_not_awaited()


def test_admin_ingest_logs_why_the_usage_could_not_be_read(
    configured: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    # A revoked Apify token must leave a trace, not only "Tente de novo mais tarde".
    refused = httpx.HTTPStatusError(
        "401", request=httpx.Request("GET", "https://api.apify.com/v2/users/me/limits"), response=httpx.Response(401)
    )

    with (
        patch("app.api.admin.start_run", AsyncMock()),
        patch("app.api.admin.monthly_usage", AsyncMock(side_effect=refused)),
        caplog.at_level(logging.WARNING, logger="app.api.admin"),
    ):
        response = TestClient(app).post("/admin/ingest", json={"source": "sympla"}, headers={"X-Admin-Token": ADMIN})

    assert response.status_code == 503
    assert response.json()["code"] == "apify_usage_unavailable"
    assert any("HTTPStatusError" in r.getMessage() and "401" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("headers", "status"),
    [({}, 401), ({"X-Admin-Token": "wrong"}, 401)],
)
def test_admin_ingest_needs_the_admin_token(configured: MagicMock, headers: dict[str, str], status: int) -> None:
    response = TestClient(app).post("/admin/ingest", json={"source": "sympla"}, headers=headers)

    assert response.status_code == status


def test_admin_ingest_rejects_unknown_sources(configured: MagicMock) -> None:
    response = TestClient(app).post(
        "/admin/ingest", json={"source": "eventbrite"}, headers={"X-Admin-Token": ADMIN}
    )

    assert response.status_code == 422


def _webhook(secret: str | None, body: dict[str, Any] | None = None) -> httpx.Response:
    headers = {"X-Apify-Webhook-Secret": secret} if secret is not None else {}
    payload = body or {
        "eventType": "ACTOR.RUN.SUCCEEDED",
        "eventData": {"actorId": "a", "actorRunId": "run-1"},
        "resource": {"id": "run-1", "status": "SUCCEEDED", "defaultDatasetId": "ds-1"},
    }
    return TestClient(app).post("/admin/ingest/apify-webhook?source=sympla", json=payload, headers=headers)


def test_the_webhook_loads_the_runs_dataset(configured: MagicMock) -> None:
    report = IngestionReport(source="sympla", fetched=3, standardized=2, upserted=2)

    with patch("app.api.admin.ingest_dataset", AsyncMock(return_value=report)) as ingest:
        response = _webhook(HOOK)

    assert response.status_code == 200
    assert response.json()["upserted"] == 2
    args = ingest.await_args.args
    assert args[2] is SOURCES["sympla"]
    assert args[3] == "ds-1"


@pytest.mark.parametrize("secret", [None, "wrong", ADMIN])
def test_the_webhook_needs_its_own_secret(configured: MagicMock, secret: str | None) -> None:
    # The admin token does not open the webhook: Apify only ever holds the webhook secret.
    with patch("app.api.admin.ingest_dataset", AsyncMock()) as ingest:
        response = _webhook(secret)

    assert response.status_code == 401
    ingest.assert_not_called()


def test_the_webhook_is_off_without_a_secret(configured: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "APIFY_WEBHOOK_SECRET", "")

    assert _webhook("").status_code == 503


def test_the_webhook_needs_a_dataset(configured: MagicMock) -> None:
    body = {"eventType": "ACTOR.RUN.SUCCEEDED", "resource": {"id": "run-1"}}

    with patch("app.api.admin.ingest_dataset", AsyncMock()) as ingest:
        response = _webhook(HOOK, body)

    assert response.status_code == 422
    ingest.assert_not_called()
