"""The Apify actors the catalogue is built from, and what each run asks for.

Google Maps places in Recife and the Sympla events in Recife, each with a charge cap
that Apify enforces on the run itself (decision 2026-10-11: ~3 USD a round). What
pleases most people comes first (decision 2026-10-19): the searches favour the
city's icons, broad leisure and popular food and drink (`taxonomy.PLACE_SEARCHES`),
only popular places come in (`keep`), and each run is loaded most popular first
(`rank`), so the moderation and keyword budgets of the import reach those first.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from app.core.config import settings
from app.services.etl.schemas import RawEvent, StandardizedEvent
from app.services.etl.taxonomy import BROAD_APPEAL_CATEGORIES, PLACE_SEARCHES
from app.services.etl.transform import popular_place, transform_google_place, transform_sympla_event

__all__ = ["AREAS", "CITY", "SOURCES", "ApifySource", "Screening"]

SourceName = Literal["google_maps", "sympla"]

CITY = "em Recife"
# Where the food and drink searches run one by one (`PlaceSearch.per_area`).
AREAS: tuple[str, ...] = (
    "em Boa Viagem, Recife",
    "no Bairro do Recife, Boa Vista e Santo Amaro, Recife",
    "nas Graças, Espinheiro e Aflitos, Recife",
    "em Casa Forte, Parnamirim e Poço, Recife",
)

# 26 searches × 20 = 520 places at most, under the 600 the cap pays for. Google
# lists the most prominent first; `keep` then leaves out the ones few people rate.
PLACES_PER_SEARCH = 20

_LAST = datetime.max.replace(tzinfo=timezone.utc)

Pairs = Sequence[tuple[RawEvent, StandardizedEvent]]


@dataclass(frozen=True)
class Screening:
    """Which items of a run come in, and what the owner should know about it."""

    keep: list[bool]
    warnings: list[str] = field(default_factory=list)


def _keep_all(pairs: Pairs) -> Screening:
    return Screening([True] * len(pairs))


def _as_loaded(ev: StandardizedEvent) -> tuple[Any, ...]:
    return ()


@dataclass(frozen=True)
class ApifySource:
    name: SourceName
    actor: str
    max_charge_usd: float
    build_input: Callable[[], dict[str, Any]]
    transform: Callable[[RawEvent], StandardizedEvent | None]
    # Which transformed items come in (it sees the whole run), and the order the run
    # is loaded in.
    screen: Callable[[Pairs], Screening] = _keep_all
    rank: Callable[[StandardizedEvent], tuple[Any, ...]] = _as_loaded

    def run_input(self) -> dict[str, Any]:
        return self.build_input()


def _place_searches() -> list[str]:
    return [
        f"{entry.term} {area}"
        for entry in PLACE_SEARCHES
        for area in (AREAS if entry.per_area else (CITY,))
    ]


def _popular_places(pairs: Pairs) -> Screening:
    # Whether the run carries counts and ratings at all: if Apify stops sending one,
    # its floor is skipped (and said) instead of every place being dropped.
    counts_known = any(ev.reviews_count is not None for _, ev in pairs)
    ratings_known = any(ev.rating is not None for _, ev in pairs)
    warnings = [
        f"{name} missing from every place: its floor was not applied"
        for name, known in (("reviewsCount", counts_known), ("totalScore", ratings_known))
        if pairs and not known
    ]
    keep = [
        popular_place(
            item,
            ev,
            counts_known=counts_known,
            ratings_known=ratings_known,
            min_reviews=settings.PLACES_MIN_REVIEWS,
            min_reviews_niche=settings.PLACES_MIN_REVIEWS_NICHE,
            min_rating=settings.PLACES_MIN_RATING,
        )
        for item, ev in pairs
    ]
    return Screening(keep, warnings)


def _most_reviewed_first(ev: StandardizedEvent) -> tuple[Any, ...]:
    # Unknown counts last, then the better rated among equals.
    return (ev.reviews_count is None, -(ev.reviews_count or 0), -(ev.rating or Decimal(0)))


def _broad_appeal_first(ev: StandardizedEvent) -> tuple[Any, ...]:
    return (ev.category not in BROAD_APPEAL_CATEGORIES, ev.event_date or _LAST)


def _google_maps_input() -> dict[str, Any]:
    return {
        "searchStringsArray": _place_searches(),
        "maxCrawledPlacesPerSearch": PLACES_PER_SEARCH,
        "language": "pt-BR",
        "countryCode": "br",
        # A paid filter (0.001 USD a place: it cut the pilot at 480 of 600); the
        # transform drops closed places instead.
        "skipClosedPlaces": False,
        "scrapePlaceDetailPage": False,
    }


def _sympla_input() -> dict[str, Any]:
    return {
        "stateFilter": "PE",
        "cityFilter": "recife",
        "eventTypeFilter": "NORMAL",
        # 70 × 0.008 USD fits under the cap; 80 went past it and Apify cut the run.
        "maxItems": 70,
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
        screen=_popular_places,
        rank=_most_reviewed_first,
    ),
    "sympla": ApifySource(
        name="sympla",
        actor="hypebridge~sympla-events-scraper",
        # 70 detailed events at 0.008 USD, plus the start fee. Nothing is left out
        # (decision 2026-10-19): only the order changes.
        max_charge_usd=0.6,
        build_input=_sympla_input,
        transform=transform_sympla_event,
        rank=_broad_appeal_first,
    ),
}
