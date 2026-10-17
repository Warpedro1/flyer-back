"""Sampling options that each OpenAI chat model accepts.

Reasoning models (gpt-5, o1, o3, o4) only take the default temperature, 1: any
other value answers 400 (seen in production with `LLM_MODEL=gpt-5-mini`, which broke
every onboarding at the LLM judge). Leaving it out is not enough: langchain-openai
0.2 then sends its own default, 0.7. For them we send 1 and ask for the least
reasoning, so the judge and the extraction stay inside their time limits (#26).
"""

from __future__ import annotations

from typing import Any

_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _name(model: str) -> str:
    return model.strip().lower().rsplit("/", 1)[-1]


def is_reasoning_model(model: str) -> bool:
    name = _name(model)
    # "gpt-5-chat-latest" is the non-reasoning chat model and takes a temperature.
    return name.startswith(_REASONING_PREFIXES) and "chat" not in name


def chat_model_kwargs(model: str, *, temperature: float) -> dict[str, Any]:
    """Keyword arguments for `ChatOpenAI` besides the model, key and timeout."""
    if not is_reasoning_model(model):
        return {"temperature": temperature}
    effort = "minimal" if _name(model).startswith("gpt-5") else "low"
    return {"temperature": 1, "reasoning_effort": effort}
