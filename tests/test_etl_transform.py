"""Deterministic transform of Google Maps places and Sympla events (Lote 5B).

No LLM: categories come from mapping tables and coordinates from the source itself
(the old transform let the LLM guess lat/long, which put pins in the wrong place).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from app.models.schemas import EVENT_CATEGORIES
from app.services.etl.taxonomy import BROAD_APPEAL_CATEGORIES, PLACE_SEARCHES
from app.services.etl.transform import (
    clean_description,
    google_category,
    google_price_level,
    place_search,
    popular_place,
    sympla_category,
    sympla_price,
    transform_google_place,
    transform_sympla_event,
)

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def google_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "title": "Parraxaxá",
        "placeId": "ChIJ-parraxaxa",
        "categoryName": "Restaurante nordestino",
        "location": {"lat": -8.1187, "lng": -34.8995},
        "address": "Av. Fernando Simões Barbosa, 1200 - Boa Viagem, Recife - PE",
        "price": "R$ 50–100",
        "totalScore": 4.6,
        "reviewsCount": 4321,
        "openingHours": [{"day": "segunda-feira", "hours": "11:30 a 22:00"}],
        "url": "https://www.google.com/maps/search/?api=1&query_place_id=ChIJ-parraxaxa",
        "imageUrl": "https://lh5.googleusercontent.com/p/parraxaxa",
        "permanentlyClosed": False,
        "temporarilyClosed": False,
        "searchString": "restaurantes em Boa Viagem, Recife",
    }
    item.update(overrides)
    return item


def sympla_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": "3529233",
        "name": "Gustavo Pardal e Ellen em Recife",
        "sourceUrl": "https://www.sympla.com.br/evento/gustavo-pardal/3529233",
        "url": "https://beta.sympla.com.br/evento/gustavo-pardal/3529233",
        "type": "NORMAL",
        "category": "ENTRETENIMENTO",
        "format": "teatro-stand-up-e-danca",
        "startDate": "2026-10-17T21:00:00-03:00",
        "endDate": "2026-10-17T23:00:00-03:00",
        "venue": {"name": "Colégio Santa Maria", "neighborhood": "Boa Viagem", "city": "Recife"},
        "location": {"latitude": -8.1121723, "longitude": -34.8972433},
        "images": {"large": "https://images.sympla.com.br/pardal-lg.jpg"},
        "detail": {
            "cancelled": False,
            "plainDescription": "Casal sensação&nbsp;das redes.\r\n\r\n\r\n\r\nComédia ao vivo.",
            "tickets": [
                {"isFree": False, "isPrivate": False, "show": True, "salePriceMonetary": {"decimal": 80, "integer": 8000}},
                {"isFree": False, "isPrivate": False, "show": True, "salePriceMonetary": {"decimal": 40, "integer": 4000}},
            ],
        },
    }
    item.update(overrides)
    return item


# ------------------------------------------------------------ categories --


def test_every_search_maps_to_a_known_category() -> None:
    assert {s.category for s in PLACE_SEARCHES} <= set(EVENT_CATEGORIES)
    assert BROAD_APPEAL_CATEGORIES <= set(EVENT_CATEGORIES)


def test_no_search_is_the_start_of_another() -> None:
    # The category comes from the search's prefix: one must never swallow another.
    terms = [s.term for s in PLACE_SEARCHES]
    assert len(set(terms)) == len(terms)
    assert not [(a, b) for a in terms for b in terms if a != b and b.startswith(a + " ")]


@pytest.mark.parametrize(
    ("search", "term", "tier"),
    [
        ("parques em Recife", "parques", "broad"),
        ("restaurantes em Boa Viagem, Recife", "restaurantes", "broad"),
        ("yoga e spa em Recife", "yoga e spa", "niche"),
        ("Pontos Turísticos em Recife", "pontos turísticos", "broad"),
        ("oficinas mecânicas em Recife", None, None),
        (None, None, None),
    ],
)
def test_the_search_that_found_a_place(search: str | None, term: str | None, tier: str | None) -> None:
    found = place_search(search)

    assert (found.term if found else None, found.tier if found else None) == (term, tier)


@pytest.mark.parametrize(
    ("search", "category_name", "expected"),
    [
        # The search that found the place decides: it was chosen for that category.
        ("academias em Recife", "Academia", "academia_exercicio"),
        ("parques em Recife", "Parque", "praia_natureza"),
        ("bares em Boa Viagem, Recife", "Restaurante", "vida_noturna"),
        # Without a known search, Google's own category is mapped.
        (None, "Livraria", "estudo_leitura"),
        (None, "Bar", "vida_noturna"),
        (None, "Shopping center", "compras_lojas"),
        (None, "Oficina mecânica", "outros"),
        (None, None, "outros"),
    ],
)
def test_google_category(search: str | None, category_name: str | None, expected: str) -> None:
    assert google_category(search, category_name) == expected


@pytest.mark.parametrize(
    ("fmt", "category", "expected"),
    [
        ("teatro-stand-up-e-danca", "ENTRETENIMENTO", "teatro_cinema"),
        ("informatica-tecnologia-e-programacao", "NEGÓCIOS", "tecnologia_negocios"),
        ("saude-nutricao-e-bemestar", "ESPORTIVO", "bem_estar"),
        ("festas-e-shows", "ENTRETENIMENTO", "musica_show"),
        ("cursos-e-workshops", "OUTRO", "cursos_oficinas"),
        ("congressos-e-palestras", "NEGÓCIOS", "palestra_conhecimento"),
        ("gastronomia", "OUTRO", "comida_bebida"),
        ("religiao-e-espiritualidade", "OUTRO", "religiao_espiritualidade"),
        ("infantil", "OUTRO", "familia_criancas"),
        ("outro", "ESPORTIVO", "desporto"),
        ("outro", "OUTRO", "outros"),
    ],
)
def test_sympla_category(fmt: str, category: str, expected: str) -> None:
    assert sympla_category(fmt, category) == expected


# ------------------------------------------------------------------ price --


@pytest.mark.parametrize(
    ("price", "level"),
    [
        ("$", 1),
        ("$$$$", 4),
        ("R$ 1–20", 1),
        ("R$ 50–100", 2),
        ("R$ 100–150", 3),
        ("R$ 100+", 3),
        ("R$ 250+", 4),
        (None, None),
        ("", None),
    ],
)
def test_google_price_level(price: str | None, level: int | None) -> None:
    assert google_price_level(price) == level


def test_sympla_price_is_the_cheapest_public_ticket_in_reais() -> None:
    # salePriceMonetary.integer is in centavos.
    assert sympla_price(sympla_item()) == Decimal("40.00")


def test_sympla_free_tickets_cost_zero() -> None:
    item = sympla_item(detail={"tickets": [{"isFree": True, "show": True, "isPrivate": False}]})

    assert sympla_price(item) == Decimal(0)


def test_sympla_without_public_tickets_has_unknown_price() -> None:
    # Unknown, not free: price = 0 would promise something the source never said.
    hidden = {"isFree": False, "show": True, "isPrivate": True, "salePriceMonetary": {"integer": 1000}}

    assert sympla_price(sympla_item(detail={"tickets": [hidden]})) is None
    assert sympla_price(sympla_item(detail={})) is None


# ----------------------------------------------------------------- places --


def test_a_google_place_becomes_a_place_with_source_coordinates() -> None:
    place = transform_google_place(google_item())

    assert place is not None
    assert place.kind == "place"
    assert place.source == "google_maps"
    assert place.source_event_id == "ChIJ-parraxaxa"
    assert (place.lat, place.long) == (-8.1187, -34.8995)
    assert place.category == "comida_bebida"
    assert place.price_level == 2
    assert place.price is None
    assert place.event_date is None
    assert place.rating == Decimal("4.6")
    assert place.reviews_count == 4321
    assert place.location_name == "Av. Fernando Simões Barbosa, 1200 - Boa Viagem, Recife - PE"
    assert [h.model_dump() for h in place.opening_hours or []] == [
        {"day": "segunda-feira", "hours": "11:30 a 22:00"}
    ]
    assert place.source_url and place.source_url.startswith("https://www.google.com/maps")
    assert place.cover_url == "https://lh5.googleusercontent.com/p/parraxaxa"


@pytest.mark.parametrize(
    "overrides",
    [
        {"location": None},
        {"location": {"lat": None, "lng": -34.9}},
        {"placeId": None},
        {"title": ""},
        {"permanentlyClosed": True},
    ],
)
def test_google_places_that_cannot_be_used_are_dropped(overrides: dict[str, Any]) -> None:
    assert transform_google_place(google_item(**overrides)) is None


@pytest.mark.parametrize("reviews", [None, "4321", True, -3, 12.5])
def test_a_review_count_that_is_not_a_count_is_unknown(reviews: Any) -> None:
    place = transform_google_place(google_item(reviewsCount=reviews))

    assert place is not None
    assert place.reviews_count is None


# ------------------------------------------------------------- popularity --

BARS = {"min_reviews": 300, "min_reviews_niche": 50, "min_rating": 4.2}


@pytest.mark.parametrize(
    ("search", "reviews", "score", "kept"),
    [
        # Decision 2026-10-19: what pleases most people, by reviews and rating.
        ("parques em Recife", 300, 4.2, True),
        ("parques em Recife", 299, 4.9, False),
        ("restaurantes em Boa Viagem, Recife", 5000, 4.1, False),
        # Niches still come, with a lower bar.
        ("yoga e spa em Recife", 50, 4.8, True),
        ("yoga e spa em Recife", 49, 4.8, False),
        ("yoga e spa em Recife", 80, 3.9, False),
        # A search we do not know gets the broad bar.
        ("oficinas mecânicas em Recife", 120, 4.5, False),
        # Unknown is not unpopular: kept (and ranked last).
        ("parques em Recife", None, 4.5, True),
        ("parques em Recife", 900, None, True),
    ],
)
def test_only_popular_places_are_kept(search: str, reviews: int | None, score: float | None, kept: bool) -> None:
    item = google_item(searchString=search, reviewsCount=reviews, totalScore=score)
    place = transform_google_place(item)

    assert place is not None
    assert popular_place(item, place, **BARS) is kept


# ----------------------------------------------------------------- events --


def test_a_sympla_event_becomes_an_event() -> None:
    event = transform_sympla_event(sympla_item(), now=NOW)

    assert event is not None
    assert event.kind == "event"
    assert event.source == "sympla"
    assert event.source_event_id == "3529233"
    assert event.title == "Gustavo Pardal e Ellen em Recife"
    assert event.category == "teatro_cinema"
    assert event.event_date == datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)
    assert event.price == Decimal("40.00")
    assert event.location_name == "Colégio Santa Maria"
    assert (event.lat, event.long) == (-8.1121723, -34.8972433)
    assert event.source_url == "https://www.sympla.com.br/evento/gustavo-pardal/3529233"
    assert event.cover_url == "https://images.sympla.com.br/pardal-lg.jpg"
    assert event.description == "Casal sensação das redes.\n\nComédia ao vivo."


def test_a_sympla_event_keeps_its_end_and_opens_check_in_at_the_start() -> None:
    # Check-in closes when the event ends (decision 2026-10-12).
    event = transform_sympla_event(sympla_item(), now=NOW)

    assert event is not None
    assert event.ends_at == datetime(2026, 10, 18, 2, 0, tzinfo=timezone.utc)
    assert event.checkin_opens_at == event.event_date


def test_without_an_end_a_sympla_event_lasts_four_hours() -> None:
    event = transform_sympla_event(sympla_item(endDate=None), now=NOW)

    assert event is not None
    assert event.ends_at == datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)


def test_an_end_before_the_start_is_replaced_by_four_hours() -> None:
    event = transform_sympla_event(sympla_item(endDate="2026-10-17T20:00:00-03:00"), now=NOW)

    assert event is not None
    assert event.ends_at == datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "overrides",
    [
        {"location": {"latitude": None, "longitude": None}},
        {"type": "ONLINE"},
        {"startDate": None},
        {"endDate": "2026-10-01T23:00:00-03:00", "startDate": "2026-10-01T21:00:00-03:00"},
        {"detail": {"cancelled": True}},
        {"id": None},
    ],
)
def test_sympla_events_that_cannot_be_used_are_dropped(overrides: dict[str, Any]) -> None:
    assert transform_sympla_event(sympla_item(**overrides), now=NOW) is None


def test_long_descriptions_are_cut_to_fit() -> None:
    text = clean_description("palavra " * 600)

    assert text is not None
    assert len(text) <= 2000
    assert text.endswith("…")
