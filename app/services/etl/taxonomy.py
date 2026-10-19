"""How source categories map onto the Flyer taxonomy (`EventCategory`).

Deterministic tables instead of an LLM: the same item always lands in the same
category, and a wrong mapping is a one-line fix with a test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.models.schemas import EventCategory

PlaceTier = Literal["broad", "niche"]


@dataclass(frozen=True)
class PlaceSearch:
    """One Google Maps search: what it looks for and the category it gives.

    The search that found a place decides its category: it was chosen for that
    category (a ``generic`` one only when Google's own category says nothing).
    ``tier`` sets how popular a place must be to come in (see
    `transform.popular_place`); ``per_area`` searches each area of `sources.AREAS`
    instead of the whole city.
    """

    term: str
    category: EventCategory
    tier: PlaceTier
    per_area: bool = False
    # A search that also finds places of other kinds (museums, parks, theatres…):
    # Google's own category decides those, so the order of the searches does not.
    generic: bool = False


# What pleases most people first (decision 2026-10-19): the city's icons and broad
# leisure for the whole city, food and drink in each area, and the niches, which
# still come, for the whole city only and with a lower bar.
PLACE_SEARCHES: tuple[PlaceSearch, ...] = (
    PlaceSearch("pontos turísticos", "turismo_passeio", "broad", generic=True),
    PlaceSearch("parques", "praia_natureza", "broad"),
    PlaceSearch("praias", "praia_natureza", "broad"),
    PlaceSearch("museus", "arte_cultura", "broad"),
    PlaceSearch("centros culturais e galerias de arte", "arte_cultura", "broad"),
    PlaceSearch("mercados públicos e feiras", "feira_mercado", "broad"),
    PlaceSearch("shoppings", "compras_lojas", "broad"),
    PlaceSearch("cinemas", "teatro_cinema", "broad"),
    PlaceSearch("teatros", "teatro_cinema", "broad"),
    PlaceSearch("casas de show", "musica_show", "broad"),
    PlaceSearch("boliche, escape room e fliperama", "jogos", "broad"),
    PlaceSearch("cafeterias e sorveterias", "comida_bebida", "broad"),
    PlaceSearch("restaurantes", "comida_bebida", "broad", per_area=True),
    PlaceSearch("bares", "vida_noturna", "broad", per_area=True),
    PlaceSearch("livrarias e bibliotecas", "estudo_leitura", "niche"),
    PlaceSearch("cursos e oficinas", "cursos_oficinas", "niche"),
    PlaceSearch("academias", "academia_exercicio", "niche"),
    PlaceSearch("quadras e centros esportivos", "desporto", "niche"),
    PlaceSearch("yoga e spa", "bem_estar", "niche"),
    PlaceSearch("jogos de tabuleiro", "jogos", "niche"),
)

# The events that go first (decision 2026-10-19: shows, parties, festivals and
# theatre; Sympla's festivals come as "festas e shows"). They come first in the
# import (so the moderation budget reaches them first) and, without a profile, in
# discovery: `match_events` repeats this list (migration 20261019000000), and a test
# keeps the two the same.
BROAD_APPEAL_CATEGORIES: frozenset[EventCategory] = frozenset({"musica_show", "vida_noturna", "teatro_cinema"})

# Keywords in Google's `categoryName` (lowercase, no accents), matched as whole words
# (an optional plural "s" allowed): "bar" must not match "barbearia". First match
# wins, so the more specific words come first.
GOOGLE_KEYWORDS: tuple[tuple[str, EventCategory], ...] = (
    ("livraria", "estudo_leitura"),
    ("biblioteca", "estudo_leitura"),
    ("museu", "arte_cultura"),
    ("galeria", "arte_cultura"),
    ("centro cultural", "arte_cultura"),
    ("teatro", "teatro_cinema"),
    ("cinema", "teatro_cinema"),
    ("casa de show", "musica_show"),
    ("casa noturna", "vida_noturna"),
    ("boate", "vida_noturna"),
    ("bar", "vida_noturna"),
    ("pub", "vida_noturna"),
    ("restaurante", "comida_bebida"),
    ("lanchonete", "comida_bebida"),
    ("cafe", "comida_bebida"),
    ("cafeteria", "comida_bebida"),
    ("padaria", "comida_bebida"),
    ("sorveteria", "comida_bebida"),
    ("pizzaria", "comida_bebida"),
    ("academia", "academia_exercicio"),
    ("crossfit", "academia_exercicio"),
    ("pilates", "bem_estar"),
    ("yoga", "bem_estar"),
    ("spa", "bem_estar"),
    ("quadra", "desporto"),
    ("estadio", "desporto"),
    ("clube", "desporto"),
    ("esporte", "desporto"),
    ("praia", "praia_natureza"),
    ("parque", "praia_natureza"),
    ("jardim", "praia_natureza"),
    ("atracao turistica", "turismo_passeio"),
    ("ponto turistico", "turismo_passeio"),
    ("monumento", "turismo_passeio"),
    ("igreja", "religiao_espiritualidade"),
    ("templo", "religiao_espiritualidade"),
    ("jogo", "jogos"),
    ("fliperama", "jogos"),
    ("escola", "cursos_oficinas"),
    ("curso", "cursos_oficinas"),
    ("feira", "feira_mercado"),
    ("mercado publico", "feira_mercado"),
    ("shopping", "compras_lojas"),
    ("loja", "compras_lojas"),
)

# Keywords in Sympla's `format` slug, then in its `category`, matched the same way.
SYMPLA_KEYWORDS: tuple[tuple[str, EventCategory], ...] = (
    ("teatro", "teatro_cinema"),
    ("stand-up", "teatro_cinema"),
    ("danca", "teatro_cinema"),
    ("cinema", "teatro_cinema"),
    ("festa", "musica_show"),
    ("show", "musica_show"),
    ("musica", "musica_show"),
    ("curso", "cursos_oficinas"),
    ("workshop", "cursos_oficinas"),
    ("oficina", "cursos_oficinas"),
    ("congresso", "palestra_conhecimento"),
    ("palestra", "palestra_conhecimento"),
    ("seminario", "palestra_conhecimento"),
    ("desenvolvimento-pessoal", "palestra_conhecimento"),
    ("informatica", "tecnologia_negocios"),
    ("tecnologia", "tecnologia_negocios"),
    ("empreendedorismo", "tecnologia_negocios"),
    ("negocio", "tecnologia_negocios"),
    ("gastronomia", "comida_bebida"),
    ("saude", "bem_estar"),
    ("bemestar", "bem_estar"),
    ("religiao", "religiao_espiritualidade"),
    ("espiritual", "religiao_espiritualidade"),
    ("infantil", "familia_criancas"),
    ("familia", "familia_criancas"),
    ("arte", "arte_cultura"),
    ("cultura", "arte_cultura"),
    ("passeio", "turismo_passeio"),
    ("turismo", "turismo_passeio"),
    ("games", "jogos"),
    ("geek", "jogos"),
    ("feira", "feira_mercado"),
    ("social", "voluntariado"),
    ("esporte", "desporto"),
    ("esportivo", "desporto"),
)
