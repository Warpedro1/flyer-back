"""Apify-backed provider (Google Events search actor).

Uses Apify's run-sync-get-dataset-items endpoint over httpx. This runs in a background
ETL job (not a per-request hot path), so creating a short-lived client here is fine.

Shaped for `johnvc/google-events-api---access-google-events-data`, which pushes ONE
dataset item per run carrying an `events` array (up to 10 listings, no pagination), so
the items are flattened here. A different actor means adjusting `_run_input` and
`_flatten` together.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.services.etl.providers.base import EventProvider
from app.services.etl.schemas import RawEvent

APIFY_BASE = "https://api.apify.com/v2"


def _date_token(days: int) -> str:
    """Map the requested window onto the actor's supported date tokens."""
    if days <= 1:
        return "date:today"
    if days <= 7:
        return "date:week"
    return "date:month"


class ApifyProvider(EventProvider):
    source = "apify"

    def __init__(
        self,
        token: str,
        actor: str,
        lang: str = "",
        country: str = "",
        canonical_location: str = "",
        timeout_seconds: float = 120.0,
    ) -> None:
        self._token = token
        self._actor = actor
        # The actor wants ISO 639-1 ("pt"), and rejects regional tags like "pt-br".
        self._lang = lang.split("-")[0].strip().lower()
        self._country = country.strip().lower()
        self._location = canonical_location.strip()
        self._timeout = timeout_seconds

    def _run_input(self, location: str, days: int) -> dict[str, Any]:
        # The city goes in `q`, which accepts free text. The actor's own `location` field
        # only takes canonical Google locations ("Lisbon,Lisbon,Portugal") and aborts the
        # run on anything else, so it is sent only when explicitly configured.
        run_input: dict[str, Any] = {
            "q": f"eventos em {location}",
            "advanced": _date_token(days),
            "max_pages": 1,
        }
        if self._location:
            run_input["location"] = self._location
        if self._lang:
            run_input["hl"] = self._lang
        if self._country:
            run_input["gl"] = self._country
        return run_input

    @staticmethod
    def _flatten(data: Any) -> list[RawEvent]:
        """One dataset item wraps many listings: return the listings, not the wrapper."""
        if not isinstance(data, list):
            return []
        events: list[RawEvent] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if item.get("error"):
                # The actor reports bad input as a dataset row, not a failed run.
                raise RuntimeError(item.get("error_message") or "Apify actor returned an error")
            nested = item.get("events")
            if isinstance(nested, list):
                # Carry the searched location down: listings have no coordinates, so the
                # transform stage needs it to estimate lat/long from the address.
                params = item.get("search_parameters")
                searched = params.get("location") if isinstance(params, dict) else None
                for ev in nested:
                    if not isinstance(ev, dict):
                        continue
                    if searched and "location" not in ev:
                        ev = {**ev, "location": searched}
                    events.append(ev)
            else:
                events.append(item)
        return events

    async def fetch(self, location: str, days: int) -> list[RawEvent]:
        url = f"{APIFY_BASE}/acts/{self._actor}/run-sync-get-dataset-items"
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout)) as client:
            r = await client.post(
                url,
                params={"token": self._token},
                json=self._run_input(location, days),
            )
            r.raise_for_status()
            data = r.json()
        return self._flatten(data)
