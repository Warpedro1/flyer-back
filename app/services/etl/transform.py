"""LLM transform: normalize noisy provider payloads into validated Flyer events.

The `source_event_id` is authoritative on the Python side (derived from the raw item),
so a hallucinated id from the model is dropped rather than inserted.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from app.core.config import settings
from app.services.ai_service import ai_service
from app.services.etl.schemas import RawEvent, StandardizedEvent

logger = logging.getLogger(__name__)

FLYER_CATEGORIES = ["music", "sport", "food", "art", "nightlife", "outdoor", "other"]

_SYSTEM_PROMPT = (
    "Você padroniza eventos para o app Flyer. Recebe um JSON {\"events\": [...]} com "
    "eventos brutos e devolve um JSON {\"events\": [...]} com os campos: "
    "source_event_id (copie exatamente o recebido), title, description (tom jovem e "
    "convidativo, em pt-BR, até 2000 chars), category (um de: "
    f"{', '.join(FLYER_CATEGORIES)}), location_name, lat (float), long (float), "
    "event_date (ISO 8601 ou null), price (número em string ou null). "
    "Não invente eventos nem ids; se faltar lat/long, use a melhor estimativa a partir "
    "do endereço. Responda APENAS com o objeto JSON."
)

_COMPACT_KEYS = (
    "title", "name", "description", "category", "type", "location", "venue", "address",
    "latitude", "longitude", "lat", "lng", "long", "startDate", "start_date",
    "date", "when", "time", "event_date", "price", "link",
)


def _stable_source_event_id(item: RawEvent) -> str:
    for key in ("id", "eventId", "event_id", "url", "link"):
        value = item.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)[:200]
    basis = json.dumps(item, sort_keys=True, default=str)
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:32]  # noqa: S324 (non-crypto id)


def _compact(item: RawEvent) -> dict[str, Any]:
    return {k: item[k] for k in _COMPACT_KEYS if k in item}


async def standardize_events(
    raw: list[RawEvent],
    source: str,
    model: str | None = None,
) -> list[StandardizedEvent]:
    """Normalize raw events via the LLM; return only rows that validate against the schema."""
    if not raw:
        return []
    model = model or settings.ETL_TRANSFORM_MODEL

    by_id: dict[str, dict[str, Any]] = {}
    for item in raw:
        sid = _stable_source_event_id(item)
        by_id[sid] = {"source_event_id": sid, **_compact(item)}

    user_content = json.dumps({"events": list(by_id.values())}, ensure_ascii=False, default=str)
    try:
        raw_out = await ai_service.json_completion(_SYSTEM_PROMPT, user_content, model)
        parsed = json.loads(raw_out)
    except Exception:
        logger.warning("ETL transform: LLM call or JSON parse failed", exc_info=True)
        return []

    items = parsed.get("events") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return []

    allowed = set(StandardizedEvent.model_fields)
    out: list[StandardizedEvent] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("source_event_id", "")).strip()
        if sid not in by_id:
            continue  # drop hallucinated / unknown ids
        clean = {k: v for k, v in entry.items() if k in allowed}
        clean["source"] = source
        clean["source_event_id"] = sid
        try:
            out.append(StandardizedEvent.model_validate(clean))
        except Exception:
            logger.debug("ETL transform: skipping invalid event (sid=%s)", sid)
    return out
