"""Transform: raw Google Maps places and Sympla events into `StandardizedEvent`.

Deterministic, no LLM. Coordinates come from the source; an item without them is
dropped instead of guessed (the old LLM transform estimated lat/long from addresses).
"""

from __future__ import annotations

import html
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from app.models.schemas import EventCategory
from app.services.etl.schemas import RawEvent, StandardizedEvent
from app.services.etl.taxonomy import GOOGLE_KEYWORDS, PLACE_SEARCHES, SYMPLA_KEYWORDS, PlaceSearch

logger = logging.getLogger(__name__)

MAX_DESCRIPTION = 2000
# Used when the source gives no end (or one before the start).
DEFAULT_EVENT_DURATION = timedelta(hours=4)


# ----------------------------------------------------------------- helpers --


def normalize(text: str | None) -> str:
    """Lowercase, no accents, single spaces: for matching, never for display."""
    if not text:
        return ""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return " ".join(plain.lower().split())


def _first_keyword(text: str, table: tuple[tuple[str, EventCategory], ...]) -> EventCategory | None:
    for keyword, category in table:
        if re.search(rf"\b{re.escape(keyword)}s?\b", text):
            return category
    return None


def clean_description(text: str | None) -> str | None:
    """Unescape HTML entities, keep paragraphs, collapse blank runs, fit 2000 chars."""
    if not text:
        return None
    plain = html.unescape(text).replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in plain.split("\n")]
    joined = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not joined:
        return None
    if len(joined) > MAX_DESCRIPTION:
        joined = joined[: MAX_DESCRIPTION - 1].rstrip() + "…"
    return joined


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] or None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _build(data: dict[str, Any]) -> StandardizedEvent | None:
    try:
        return StandardizedEvent(**data)
    except ValidationError as exc:
        logger.warning("ETL item dropped (%s %s): %s", data.get("source"), data.get("source_event_id"), exc)
        return None


# ------------------------------------------------------------ categories --


def place_search(search: str | None) -> PlaceSearch | None:
    """Which of our searches found the place (Apify echoes it in ``searchString``)."""
    wanted = normalize(search)
    for entry in PLACE_SEARCHES:
        if wanted.startswith(normalize(entry.term) + " "):
            return entry
    return None


def google_category(search: str | None, category_name: str | None) -> EventCategory:
    """The search that found the place, else Google's own category, else `outros`.

    A generic search ("pontos turísticos") yields to Google's own category: a museum
    it found is still a museum, whichever of the two searches Apify output first.
    """
    found = place_search(search)
    own = _first_keyword(normalize(category_name), GOOGLE_KEYWORDS)
    if found and not (found.generic and own):
        return found.category
    return own or "outros"


def sympla_category(fmt: str | None, category: str | None) -> EventCategory:
    """Sympla's `format` slug is the more specific; its `category` is the fallback."""
    for text in (normalize(fmt), normalize(category)):
        found = _first_keyword(text, SYMPLA_KEYWORDS)
        if found:
            return found
    return "outros"


# ------------------------------------------------------------------ price --


def google_price_level(price: str | None) -> int | None:
    """Google gives "$$" or a range in reais ("R$ 50–100", "R$ 100+"): map to 1..4.

    Bands (decision 2026-10-11): up to R$50 → 1, up to 100 → 2, up to 200 → 3, more → 4.
    """
    if not price:
        return None
    numbers = [int(n) for n in re.findall(r"\d+", price)]
    if not numbers:
        signs = price.count("$")
        return min(signs, 4) if signs else None
    if "+" in price:
        low = min(numbers)
        return 4 if low >= 200 else 3 if low >= 100 else 2 if low >= 50 else 1
    high = max(numbers)
    return 1 if high <= 50 else 2 if high <= 100 else 3 if high <= 200 else 4


def sympla_price(item: RawEvent) -> Decimal | None:
    """Cheapest public ticket in reais; 0 when free; None when the source doesn't say."""
    tickets = _dict(item.get("detail")).get("tickets")
    if not isinstance(tickets, list):
        return None
    prices: list[Decimal] = []
    for ticket in tickets:
        if not isinstance(ticket, dict) or ticket.get("isPrivate") or ticket.get("show") is False:
            continue
        if ticket.get("isFree"):
            prices.append(Decimal(0))
            continue
        cents = _dict(ticket.get("salePriceMonetary")).get("integer")
        if isinstance(cents, int) and not isinstance(cents, bool) and cents >= 0:
            prices.append((Decimal(cents) / 100).quantize(Decimal("0.01")))
    return min(prices) if prices else None


# ----------------------------------------------------------------- places --


def transform_google_place(item: RawEvent) -> StandardizedEvent | None:
    """A Google Maps place (compass/crawler-google-places) as `kind = place`."""
    # Closed places are dropped here, not by the run: Apify charges its filters.
    if item.get("permanentlyClosed") or item.get("temporarilyClosed"):
        return None
    location = _dict(item.get("location"))
    lat, lng = _float(location.get("lat")), _float(location.get("lng"))
    place_id = item.get("placeId")
    title = _text(item.get("title"), 200)
    if lat is None or lng is None or not isinstance(place_id, str) or not place_id or not title:
        return None

    hours = item.get("openingHours")
    opening_hours = None
    if isinstance(hours, list):
        opening_hours = [
            {"day": str(h["day"])[:40], "hours": str(h["hours"])[:120]}
            for h in hours
            if isinstance(h, dict) and h.get("day") and h.get("hours")
        ] or None

    score = _float(item.get("totalScore"))
    return _build(
        {
            "source": "google_maps",
            "source_event_id": place_id,
            "kind": "place",
            "title": title,
            "description": clean_description(_str(item.get("description"))),
            "category": google_category(_str(item.get("searchString")), _str(item.get("categoryName"))),
            "location_name": _text(item.get("address"), 200),
            "lat": lat,
            "long": lng,
            "price_level": google_price_level(_str(item.get("price"))),
            "rating": Decimal(str(score)).quantize(Decimal("0.1")) if score is not None else None,
            "reviews_count": _count(item.get("reviewsCount")),
            "opening_hours": opening_hours,
            "source_url": _text(item.get("url"), 2000),
            "cover_url": _text(item.get("imageUrl"), 2000),
        }
    )


def popular_place(
    item: RawEvent,
    place: StandardizedEvent,
    *,
    counts_known: bool,
    ratings_known: bool,
    min_reviews: int,
    min_reviews_niche: int,
    min_rating: float,
) -> bool:
    """Whether a place is popular enough to come in (decision 2026-10-19).

    Enough Google reviews for its search's tier (a search we do not know gets the
    broad bar) and a good rating. ``counts_known``/``ratings_known`` say whether the
    run carries that signal at all: when it does, a place without it has none (no
    reviews, no rating); when the whole run lacks it, that floor cannot be applied.
    """
    if counts_known:
        found = place_search(_str(item.get("searchString")))
        needed = min_reviews_niche if found and found.tier == "niche" else min_reviews
        if (place.reviews_count or 0) < needed:
            return False
    if ratings_known and (place.rating is None or float(place.rating) < min_rating):
        return False
    return True


# ----------------------------------------------------------------- events --


def transform_sympla_event(item: RawEvent, now: datetime | None = None) -> StandardizedEvent | None:
    """A Sympla event (hypebridge/sympla-events-scraper) as `kind = event`."""
    now = now or datetime.now(timezone.utc)
    detail = _dict(item.get("detail"))
    if detail.get("cancelled") or item.get("type") not in (None, "NORMAL"):
        return None  # online / on-demand events have no place to go to
    location = _dict(item.get("location"))
    lat, lng = _float(location.get("latitude")), _float(location.get("longitude"))
    event_id = item.get("id")
    title = _text(item.get("name"), 200)
    start = _parse_dt(item.get("startDate"))
    if lat is None or lng is None or event_id in (None, "") or not title or start is None:
        return None
    end = _parse_dt(item.get("endDate"))
    if end is None or end <= start:
        # Check-in closes at the end: without a usable one, four hours (decision 2026-10-12).
        end = start + DEFAULT_EVENT_DURATION
    if end < now:
        return None

    venue = _dict(item.get("venue"))
    images = _dict(item.get("images"))
    return _build(
        {
            "source": "sympla",
            "source_event_id": str(event_id),
            "kind": "event",
            "title": title,
            "description": clean_description(_str(detail.get("plainDescription"))),
            "category": sympla_category(_str(item.get("format")), _str(item.get("category"))),
            "location_name": _text(venue.get("name"), 200),
            "lat": lat,
            "long": lng,
            "event_date": start,
            "ends_at": end,
            "checkin_opens_at": start,
            "price": sympla_price(item),
            "source_url": _text(item.get("sourceUrl") or item.get("url"), 2000),
            "cover_url": _text(images.get("large") or images.get("original"), 2000),
        }
    )
