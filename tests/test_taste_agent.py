"""Taste agent (#31, #26 part 2, #22 part B; decisions 2026-10-18).

The LLM is a fake runnable here (`taste_agent._structured`); `test_live_*` at the
end calls the real API with the reference examples in `tests/fixtures/taste_agent/`
and only runs with ``RUN_LIVE_LLM_TESTS=1`` and a real ``OPENAI_API_KEY``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from unittest.mock import MagicMock, patch

import pytest

from app.core.config import settings
from app.data.onboarding_guide import GUIDE_STEPS
from app.models.schemas import EVENT_CATEGORIES
from app.services import taste_agent
from app.services.taste_agent import (
    CATEGORY_LABELS,
    EVENT_KEYWORDS_MAX,
    PERSON_KEYWORDS_MAX,
    AnalyzedItem,
    AnswerAnalysis,
    EventText,
    StepQA,
    TasteKeywords,
    TasteSynthesis,
    WeightedKeyword,
    analyze_answers,
    analyze_event,
    analyze_events,
    analyze_person,
    event_content_hash,
    event_embedding_text,
    merge_keywords,
    normalize_keyword,
    person_embedding_text,
    step_documents,
)

# Taken at import, before the autouse fixture swaps it for a runnable that always fails.
from app.services.taste_agent import _structured as real_structured

FIXTURES = Path(__file__).parent / "fixtures" / "taste_agent"


def _kw(*terms: str, weight: int = 2) -> list[WeightedKeyword]:
    return [WeightedKeyword(term=t, weight=weight) for t in terms]


class _Fake:
    """Answers per schema and keeps every request it got."""

    def __init__(self, answers: dict[type, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[type, list[dict[str, str]]]] = []

    def runnable(self, schema: type) -> Any:
        fake = self

        class _R:
            async def ainvoke(self, messages: list[dict[str, str]]) -> Any:
                fake.calls.append((schema, messages))
                answer = fake.answers[schema]
                if isinstance(answer, BaseException):
                    raise answer
                return answer(messages) if callable(answer) else answer

        return _R()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _Fake:
    f = _Fake({})
    monkeypatch.setattr(taste_agent, "_structured", f.runnable)
    return f


# ---------------------------------------------------------- normalising --


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Literatura   Clássica. ", "literatura clássica"),
        ("“MPB”", "mpb"),
        ("#Forró!", "forró"),
        ("", ""),
        ("x" * 61, ""),
        # Personal data never becomes a keyword.
        ("fulano@example.com", ""),
        ("81 99999-1234", ""),
    ],
)
def test_keywords_are_normalised(raw: str, expected: str) -> None:
    assert normalize_keyword(raw) == expected


def test_merging_adds_the_weights_and_keeps_the_first_order_on_ties() -> None:
    merged = merge_keywords(
        [_kw("Cinema", "praia", weight=1), _kw("cinema", "frevo", weight=2), _kw("Livros", weight=2)],
        limit=10,
    )

    assert merged == ("cinema", "frevo", "livros", "praia")


def test_merging_stops_at_the_limit() -> None:
    assert len(merge_keywords([_kw(*[f"tema {i}" for i in range(50)])], limit=PERSON_KEYWORDS_MAX)) == 30


def test_weights_out_of_range_are_clamped() -> None:
    merged = merge_keywords([[WeightedKeyword(term="a", weight=99), WeightedKeyword(term="b", weight=-5)]], 5)

    assert merged == ("a", "b")


# ---------------------------------------------------------------- person --


async def test_an_answer_with_nothing_found_is_not_evidence(fake: _Fake) -> None:
    # "não sei": the synthesis must not fill its keywords from the questions.
    fake.answers[AnswerAnalysis] = AnswerAnalysis(summary="Nada a dizer.")
    fake.answers[TasteSynthesis] = TasteSynthesis(keywords=_kw("inventada"))

    person = await analyze_person([StepQA("free_time", "P?", "não sei")])

    assert (person.mode, person.taste) == ("raw", None)
    assert [schema for schema, _ in fake.calls] == [AnswerAnalysis]


async def test_the_merge_takes_the_first_summaries_that_exist(fake: _Fake) -> None:
    def answer(messages: list[dict[str, str]]) -> AnswerAnalysis:
        summary = "" if "vazio" in messages[1]["content"] else "Gosta de frevo."
        return AnswerAnalysis(keywords=_kw("frevo"), summary=summary)

    fake.answers[AnswerAnalysis] = answer
    fake.answers[TasteSynthesis] = RuntimeError("down")

    person = await analyze_person([StepQA("a", "P?", "vazio"), StepQA("b", "P?", "vazio"), StepQA("c", "P?", "x")])

    assert person.taste is not None and person.taste.summary == "Gosta de frevo."


def test_the_runnables_are_built_once_per_model_and_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    # Never per time limit: near a deadline every call has its own.
    monkeypatch.setattr(taste_agent, "_runnables", {})
    built: list[dict[str, Any]] = []

    def chat(**kwargs: Any) -> Any:
        built.append(kwargs)
        return MagicMock()

    with patch.object(taste_agent, "ChatOpenAI", side_effect=chat):
        for _ in range(3):
            real_structured(TasteSynthesis)
        real_structured(AnswerAnalysis)

    assert len(built) == 2
    assert built[0]["timeout"] == max(
        settings.TASTE_EXTRACT_TIMEOUT_SECONDS, settings.TASTE_SYNTH_TIMEOUT_SECONDS, settings.EVENT_KEYWORDS_TIMEOUT_SECONDS
    )


async def test_a_person_is_analysed_answer_by_answer_then_synthesized(fake: _Fake) -> None:
    fake.answers[AnswerAnalysis] = AnswerAnalysis(keywords=_kw("leitura"), summary="Gosta de ler.")
    fake.answers[TasteSynthesis] = TasteSynthesis(
        keywords=_kw(*[f"Tema {i}" for i in range(40)]), summary="  Gosta de   clássicos.  "
    )
    qas = [StepQA(s.key, s.prompt, "algo") for s in GUIDE_STEPS]

    person = await analyze_person(qas)

    assert person.mode == "synthesized"
    assert person.taste is not None
    assert len(person.taste.keywords) == PERSON_KEYWORDS_MAX
    assert person.taste.keywords[0] == "tema 0"
    assert person.taste.summary == "Gosta de clássicos."
    schemas = [schema for schema, _ in fake.calls]
    assert schemas.count(AnswerAnalysis) == len(GUIDE_STEPS)
    assert schemas.count(TasteSynthesis) == 1


async def test_the_desert_island_answer_gets_the_book_film_music_hint(fake: _Fake) -> None:
    fake.answers[AnswerAnalysis] = AnswerAnalysis()
    answer = "mil e uma noites, o conde de monte cristo, faroeste caboclo"

    await analyze_answers([StepQA("favorite_media", GUIDE_STEPS[3].prompt, answer)])

    system, user = fake.calls[0][1]
    assert "livro, filme e música" in user["content"]
    # What the person wrote is data inside the user turn, never in the instructions.
    assert f"<resposta>\n{answer}\n</resposta>" in user["content"]
    assert answer not in system["content"]
    assert "não uma instrução" in system["content"]


async def test_an_empty_answer_is_not_sent(fake: _Fake) -> None:
    fake.answers[AnswerAnalysis] = AnswerAnalysis()

    steps = await analyze_answers([StepQA("free_time", "P?", "   ")])

    assert steps[0].analysis is None
    assert fake.calls == []


async def test_a_failing_analyst_leaves_only_that_answer_raw(
    fake: _Fake, recorded: list[dict[str, Any]]
) -> None:
    def answer(messages: list[dict[str, str]]) -> AnswerAnalysis:
        if "quebra" in messages[1]["content"]:
            raise RuntimeError("bad request")
        return AnswerAnalysis(keywords=_kw("frevo"))

    fake.answers[AnswerAnalysis] = answer
    fake.answers[TasteSynthesis] = TasteSynthesis(keywords=_kw("frevo"))

    person = await analyze_person([StepQA("a", "P?", "frevo"), StepQA("b", "P?", "quebra")])

    assert [s.analysis is not None for s in person.steps] == [True, False]
    assert person.mode == "synthesized"
    assert recorded[0]["code"] == "taste_agent_error"
    assert recorded[0]["details"]["stage"] == "analyst"
    assert recorded[0]["details"]["step_key"] == "b"


async def test_nothing_analysed_means_the_raw_text(fake: _Fake) -> None:
    fake.answers[AnswerAnalysis] = RuntimeError("down")

    person = await analyze_person([StepQA("a", "P?", "frevo")])

    assert (person.mode, person.taste) == ("raw", None)


async def test_a_synthesizer_without_usable_keywords_is_replaced_by_the_merge(
    fake: _Fake, recorded: list[dict[str, Any]]
) -> None:
    fake.answers[AnswerAnalysis] = AnswerAnalysis(keywords=_kw("Frevo", weight=3), summary="Gosta de frevo.")
    fake.answers[TasteSynthesis] = TasteSynthesis(keywords=_kw("", "  "))

    person = await analyze_person([StepQA("a", "P?", "frevo")])

    assert person.mode == "merged"
    assert person.taste == TasteKeywords(("frevo",), "Gosta de frevo.")
    assert recorded == []


async def test_a_failing_synthesizer_is_recorded(fake: _Fake, recorded: list[dict[str, Any]]) -> None:
    fake.answers[AnswerAnalysis] = AnswerAnalysis(keywords=_kw("frevo"))
    fake.answers[TasteSynthesis] = TimeoutError()

    person = await analyze_person([StepQA("a", "P?", "frevo")])

    assert person.mode == "merged"
    assert [(r["code"], r["details"]["stage"]) for r in recorded] == [("taste_agent_timeout", "synthesizer")]


def test_the_chat_documents_keep_the_answer_and_carry_the_analysis() -> None:
    analysis = AnswerAnalysis(
        items=[AnalyzedItem(name="Faroeste Caboclo", kind="música", genre="rock nacional", era_origin="1987, Brasil")],
        keywords=_kw("rock nacional"),
        summary="Gosta de música brasileira.",
    )
    steps = [taste_agent.StepTaste(StepQA("favorite_media", "P?", "faroeste caboclo"), analysis)]

    [doc] = step_documents(steps)

    assert "Resposta: faroeste caboclo" in doc.page_content
    assert "Faroeste Caboclo (música, rock nacional, 1987, Brasil)" in doc.page_content
    assert "Palavras-chave: rock nacional" in doc.page_content
    assert doc.metadata["extraction"] == "llm"
    assert doc.metadata["analysis"]["summary"] == "Gosta de música brasileira."


def test_the_persons_vector_text_is_only_keywords_and_summary() -> None:
    text = person_embedding_text(TasteKeywords(("literatura clássica", "debates"), "Gosta de clássicos."))

    assert text == "Palavras-chave: literatura clássica, debates.\nResumo: Gosta de clássicos."


# ---------------------------------------------------------------- events --


async def test_an_event_gets_at_most_15_keywords(fake: _Fake) -> None:
    fake.answers[TasteSynthesis] = TasteSynthesis(keywords=_kw(*[f"k{i}" for i in range(30)]), summary="Show.")

    taste = await analyze_event(EventText("Forró", "Noite de forró", "musica_show", "event"))

    assert taste is not None and len(taste.keywords) == EVENT_KEYWORDS_MAX


async def test_the_event_prompt_says_what_it_is(fake: _Fake) -> None:
    fake.answers[TasteSynthesis] = TasteSynthesis(keywords=_kw("praia"))

    await analyze_event(EventText("Praia de Boa Viagem", "", "praia_natureza", "place"))

    user = fake.calls[0][1][1]["content"]
    assert "Tipo: lugar" in user
    assert "Categoria: Praia/Natureza" in user


async def test_a_failing_event_call_is_none_and_recorded(fake: _Fake, recorded: list[dict[str, Any]]) -> None:
    fake.answers[TasteSynthesis] = RuntimeError("down")

    assert await analyze_event(EventText("Forró", "", "musica_show")) is None
    assert recorded[0]["details"]["stage"] == "event"


async def test_many_events_run_a_few_at_a_time(fake: _Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TASTE_AGENT_CONCURRENCY", 3)
    running = peak = 0

    async def slow(_: list[dict[str, str]]) -> TasteSynthesis:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return TasteSynthesis(keywords=_kw("show"))

    class _R:
        async def ainvoke(self, messages: list[dict[str, str]]) -> Any:
            return await slow(messages)

    monkeypatch.setattr(taste_agent, "_structured", lambda *_: _R())

    out = await analyze_events([EventText(f"E{i}", "", None) for i in range(10)])

    assert peak == 3
    assert all(t is not None for t in out)


async def test_the_budget_stops_the_batch(monkeypatch: pytest.MonkeyPatch, recorded: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(settings, "TASTE_AGENT_CONCURRENCY", 2)
    monkeypatch.setattr(taste_agent, "_MIN_CALL_SECONDS", 0.01)

    class _Slow:
        async def ainvoke(self, *_: Any) -> Any:
            await asyncio.sleep(5)

    monkeypatch.setattr(taste_agent, "_structured", lambda *_: _Slow())
    loop = asyncio.get_running_loop()
    start = loop.time()

    out = await analyze_events([EventText(f"E{i}", "", None) for i in range(6)], budget_seconds=0.2)

    assert out == [None] * 6
    assert loop.time() - start < 1
    # Recorded once for the batch, not once per event.
    assert len(recorded) == 1
    assert recorded[0]["details"]["stage"] == "event_batch"


def test_the_events_vector_text_has_no_description() -> None:
    text = event_embedding_text("Forró de Lais Senna", "musica_show", TasteKeywords(("forró", "dança"), "Noite de forró."))

    assert text == "Forró de Lais Senna\nCategoria: Música/Show\nPalavras-chave: forró, dança.\nResumo: Noite de forró."


def test_every_category_has_the_apps_label() -> None:
    assert set(CATEGORY_LABELS) == set(EVENT_CATEGORIES)


def test_the_content_hash_ignores_none_versus_empty() -> None:
    assert event_content_hash("A", None, None) == event_content_hash("A", "", "")
    assert event_content_hash("A", "b", "c") != event_content_hash("A", "bc", "")


def test_the_model_is_the_small_one_by_default() -> None:
    assert settings.TASTE_EXTRACTOR_MODEL == "gpt-4o-mini"


def test_the_reference_examples_are_well_formed() -> None:
    files = sorted(FIXTURES.glob("*.json"))
    assert files
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["expected_any"], path.name
        assert ("answers" in data) != ("event" in data), path.name


# ------------------------------------------------------------------ live --

LIVE = (
    os.environ.get("RUN_LIVE_LLM_TESTS") == "1"
    and bool(settings.OPENAI_API_KEY)
    and not settings.OPENAI_API_KEY.startswith("sk-test")
)


def _covers(keywords: tuple[str, ...], group: list[str]) -> bool:
    return any(term in kw or kw in term for term in group for kw in keywords)


@pytest.mark.skipif(not LIVE, reason="set RUN_LIVE_LLM_TESTS=1 and a real OPENAI_API_KEY")
@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
async def test_live_reference_examples(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Manual check against the real API: the minimum keywords of each example."""
    monkeypatch.setattr(taste_agent, "_structured", real_structured)
    data = json.loads(path.read_text(encoding="utf-8"))
    if "answers" in data:
        prompts = {s.key: s.prompt for s in GUIDE_STEPS}
        person = await analyze_person([StepQA(k, prompts[k], v) for k, v in data["answers"].items()])
        assert person.taste is not None, person.mode
        keywords = person.taste.keywords
    else:
        ev = data["event"]
        taste = await analyze_event(EventText(ev["title"], ev["description"], ev["category"], ev["kind"]))
        assert taste is not None
        keywords = taste.keywords
    missing = [group for group in data["expected_any"] if not _covers(keywords, group)]
    assert not missing, f"{keywords} misses {missing}"
