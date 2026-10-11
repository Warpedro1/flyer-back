"""How source categories map onto the Flyer taxonomy (`EventCategory`).

Deterministic tables instead of an LLM: the same item always lands in the same
category, and a wrong mapping is a one-line fix with a test.
"""

from __future__ import annotations

from app.models.schemas import EventCategory

# (Google Maps search, category). The search that found a place decides its category:
# it was chosen for that category. Used by the pilot run in `sources.py`.
PILOT_SEARCHES: tuple[tuple[str, EventCategory], ...] = (
    ("restaurantes", "comida_bebida"),
    ("bares", "vida_noturna"),
    ("casas de show", "musica_show"),
    ("museus e galerias de arte", "arte_cultura"),
    ("teatros e cinemas", "teatro_cinema"),
    ("livrarias e bibliotecas", "estudo_leitura"),
    ("cursos e oficinas", "cursos_oficinas"),
    ("academias", "academia_exercicio"),
    ("quadras e centros esportivos", "desporto"),
    ("yoga e spa", "bem_estar"),
    ("praias e parques", "praia_natureza"),
    ("pontos turísticos", "turismo_passeio"),
    ("jogos de tabuleiro e fliperamas", "jogos"),
    ("shoppings e lojas", "compras_lojas"),
    ("feiras e mercados públicos", "feira_mercado"),
)

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
