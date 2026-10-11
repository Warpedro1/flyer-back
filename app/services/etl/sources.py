"""The Apify actors the catalogue is built from, and what each run asks for.

Pilot (decision 2026-10-11, budget ~3 USD): Google Maps places in four areas of
Recife, one search per category, and the Sympla events in Recife. Each source has a
charge cap that Apify enforces on the run itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from app.services.etl.schemas import RawEvent, StandardizedEvent
from app.services.etl.taxonomy import PILOT_SEARCHES
from app.services.etl.transform import transform_google_place, transform_sympla_event

__all__ = ["PILOT_AREAS", "PILOT_SEARCHES", "SOURCES", "ApifySource"]

SourceName = Literal["google_maps", "sympla"]

PILOT_AREAS: tuple[str, ...] = (
    "em Boa Viagem, Recife",
    "no Bairro do Recife, Boa Vista e Santo Amaro, Recife",
    "nas Graças, Espinheiro e Aflitos, Recife",
    "em Casa Forte, Parnamirim e Poço, Recife",
)

PLACES_PER_SEARCH = 10


@dataclass(frozen=True)
class ApifySource:
    name: SourceName
    actor: str
    max_charge_usd: float
    build_input: Callable[[], dict[str, Any]]
    transform: Callable[[RawEvent], StandardizedEvent | None]

    def run_input(self) -> dict[str, Any]:
        return self.build_input()


def _google_maps_input() -> dict[str, Any]:
    return {
        "searchStringsArray": [f"{search} {area}" for search, _ in PILOT_SEARCHES for area in PILOT_AREAS],
        "maxCrawledPlacesPerSearch": PLACES_PER_SEARCH,
        "language": "pt-BR",
        "countryCode": "br",
        "skipClosedPlaces": True,
        "scrapePlaceDetailPage": False,
    }


def _sympla_input() -> dict[str, Any]:
    return {
        "stateFilter": "PE",
        "cityFilter": "recife",
        "eventTypeFilter": "NORMAL",
        "maxItems": 80,
        # Details carry the ticket prices; without them every price is unknown.
        "getEventDetails": True,
    }


SOURCES: dict[str, ApifySource] = {
    "google_maps": ApifySource(
        name="google_maps",
        actor="compass~crawler-google-places",
        # 600 places at 0.004 USD.
        max_charge_usd=2.4,
        build_input=_google_maps_input,
        transform=transform_google_place,
    ),
    "sympla": ApifySource(
        name="sympla",
        actor="hypebridge~sympla-events-scraper",
        # 80 detailed events at 0.008 USD, plus the start fee.
        max_charge_usd=0.6,
        build_input=_sympla_input,
        transform=transform_sympla_event,
    ),
}
