"""Per-step taste extraction (LLM for elaborate answers, light path for short ones)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from langchain_core.documents import Document
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from app.core.config import settings

logger = logging.getLogger(__name__)


class TasteSignals(BaseModel):
    """Structured taste signals for one onboarding answer."""

    themes: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    activity_types: list[str] = Field(default_factory=list)
    music_film_lit_hits: list[str] = Field(default_factory=list)
    food_cuisine: list[str] = Field(default_factory=list)
    weekend_style: str = ""
    free_text_summary: str = Field(
        default="",
        description="2–4 frases em pt-BR, factual, sem inventar factos não ditos.",
    )


@dataclass(frozen=True, slots=True)
class StepQA:
    step_key: str
    prompt: str
    answer: str


_structured_extractor = None


def _get_structured_extractor():
    """LCEL-ready Runnable (`with_structured_output`) reused across steps."""
    global _structured_extractor
    if _structured_extractor is None:
        model = (settings.TASTE_EXTRACTOR_MODEL or "").strip() or settings.LLM_MODEL
        llm = ChatOpenAI(
            api_key=settings.OPENAI_API_KEY,
            model=model,
            timeout=settings.TASTE_EXTRACT_TIMEOUT_SECONDS,
            temperature=0.2,
        )
        _structured_extractor = llm.with_structured_output(TasteSignals)
    return _structured_extractor


def _short_document(qa: StepQA) -> Document:
    body = (
        f"Pergunta ({qa.step_key}): {qa.prompt}\n"
        f"Resposta (breve): {qa.answer.strip()}\n"
        "Notas: resposta curta; poucos sinais semânticos explícitos."
    )
    return Document(
        page_content=body,
        metadata={
            "step_key": qa.step_key,
            "source": "onboarding",
            "extraction": "short",
        },
    )


async def _extract_elaborate(qa: StepQA) -> TasteSignals:
    chain = _get_structured_extractor()
    system = (
        "Extrai gostos e restrições implícitas a partir de uma pergunta de onboarding e da "
        "resposta do utilizador (pt-BR). Não inventes factos. Usa listas curtas quando aplicável."
    )
    user = f"step_key={qa.step_key}\nPergunta: {qa.prompt}\nResposta: {qa.answer}"
    return await chain.ainvoke(
        [{"role": "system", "content": system}, {"role": "user", "content": user}]
    )


def _signals_to_document(qa: StepQA, sig: TasteSignals) -> Document:
    parts = [
        f"Pergunta ({qa.step_key}): {qa.prompt}",
        f"Resposta: {qa.answer.strip()}",
    ]
    if sig.free_text_summary.strip():
        parts.append(f"Resumo: {sig.free_text_summary.strip()}")
    if sig.themes:
        parts.append("Temas: " + ", ".join(sig.themes))
    if sig.activity_types:
        parts.append("Atividades: " + ", ".join(sig.activity_types))
    if sig.music_film_lit_hits:
        parts.append("Media: " + ", ".join(sig.music_film_lit_hits))
    if sig.food_cuisine:
        parts.append("Comida: " + ", ".join(sig.food_cuisine))
    if sig.weekend_style.strip():
        parts.append("Estilo de fim de semana: " + sig.weekend_style.strip())
    if sig.entities:
        parts.append("Entidades: " + ", ".join(sig.entities))
    body = "\n".join(parts)
    return Document(
        page_content=body,
        metadata={
            "step_key": qa.step_key,
            "source": "onboarding",
            "extraction": "llm",
        },
    )


async def extract_tastes_per_step(qas: list[StepQA]) -> list[Document]:
    """Return one LangChain `Document` per step.

    A per-step extraction failure degrades to the light `_short_document` for that
    step rather than aborting the whole onboarding (the raw answer is still kept).
    """
    sem = asyncio.Semaphore(4)
    out: list[Document | None] = [None] * len(qas)

    async def one(idx: int, qa: StepQA) -> None:
        if len(qa.answer.strip()) < settings.ONBOARDING_ANSWER_ELABORATE_MIN_CHARS:
            out[idx] = _short_document(qa)
            return
        try:
            async with sem:
                sig = await _extract_elaborate(qa)
                out[idx] = _signals_to_document(qa, sig)
        except Exception:
            logger.exception("Taste extraction failed for step %s; using short document.", qa.step_key)
            out[idx] = _short_document(qa)

    await asyncio.gather(*(one(i, qa) for i, qa in enumerate(qas)))
    return [d for d in out if d is not None]


def build_canonical_taste_corpus(documents: list[Document]) -> str:
    """Single pt-BR block for the interest embedding."""
    header = "Preferências declaradas no onboarding (síntese canónica):\n"
    blocks: list[str] = []
    for doc in documents:
        key = str(doc.metadata.get("step_key", ""))
        blocks.append(f"## {key}\n{doc.page_content.strip()}")
    return header + "\n\n".join(blocks)
