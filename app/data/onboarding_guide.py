"""Server-side onboarding guide steps (mirror of Flyer `src/utils/chatGuide.ts`)."""

from __future__ import annotations

from dataclasses import dataclass

GUIDE_SCHEMA_VERSION = 1

GUIDE_STEP_KEYS: tuple[str, ...] = (
    "free_time",
    "ideal_weekend",
    "hobby_to_start",
    "favorite_media",
    "one_food_forever",
    "sleep_preference",
)


@dataclass(frozen=True, slots=True)
class GuideStep:
    key: str
    prompt: str


GUIDE_STEPS: tuple[GuideStep, ...] = (
    GuideStep("free_time", "O que você mais gosta de fazer no seu tempo livre?"),
    GuideStep("ideal_weekend", "Como você imagina que seria o fim de semana ideal?"),
    GuideStep(
        "hobby_to_start",
        "Se você tivesse que fazer uma atividade nova, o que seria?",
    ),
    GuideStep(
        "favorite_media",
        "Se você estivesse indo para uma ilha deserta e só pudesse levar um livro, "
        "um filme e uma música, quais seriam?",
    ),
    GuideStep(
        "one_food_forever",
        "Se só pudesse comer uma comida pelo resto da vida, qual seria?",
    ),
    GuideStep(
        "sleep_preference",
        "Você prefere acordar cedo ou dormir até tarde?",
    ),
)

STEP_LABELS: dict[str, str] = {
    "free_time": "Tempo livre",
    "ideal_weekend": "Fim de semana ideal",
    "hobby_to_start": "Hobby que gostaria de começar",
    "favorite_media": "Filme, livro ou música",
    "one_food_forever": "Comida para o resto da vida",
    "sleep_preference": "Rotina de sono",
}


def build_preferences_markdown(answers: dict[str, str]) -> str:
    """Short bullet summary (also stored for chat injection)."""
    lines: list[str] = []
    for step in GUIDE_STEPS:
        v = (answers.get(step.key) or "").strip()
        if v:
            label = STEP_LABELS.get(step.key, step.key)
            lines.append(f"- {label}: {v}")
    return "\n".join(lines)


def build_guard_corpus(answers: dict[str, str]) -> str:
    """Full Q&A text derived only from official prompts + user answers (server truth)."""
    blocks: list[str] = []
    for step in GUIDE_STEPS:
        ans = (answers.get(step.key) or "").strip()
        blocks.append(f"### {step.key}\nPergunta: {step.prompt}\nResposta: {ans}")
    return "\n\n".join(blocks)
