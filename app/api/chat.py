"""AI chat endpoints."""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import DbServiceDep, get_current_user_with_token
from app.core.limiter import limiter
from app.models.schemas import EMBEDDING_DIMENSIONS, ChatMessageIn, ChatMessageOut, ChatSender
from app.services.ai_service import ai_service

router = APIRouter(prefix="/chat", tags=["chat"])

CHAT_SYSTEM_PROMPT = """Es um assistente inteligente da plataforma Flyer.
Ajuda o utilizador a descobrir eventos, atividades e experiencias
com base nos seus interesses e localizacao.
Responde de forma breve, util e amigavel."""


def _build_embedding_corpus(messages: list[dict[str, Any]]) -> str:
    """Structured dialogue for sync; emphasises user-authored lines for interest embedding."""
    dialogue_lines: list[str] = []
    user_lines: list[str] = []
    for m in messages:
        sender = str(m.get("sender", ""))
        content = str(m.get("content", ""))
        line = f"{sender.capitalize()}: {content}"
        dialogue_lines.append(line)
        if sender == ChatSender.user.value:
            user_lines.append(f"User: {content}")
    dialog = "\n".join(dialogue_lines)
    user_block = "\n".join(user_lines)
    if user_block:
        return f"{user_block}\n\n---\n{dialog}"
    return dialog


def _parse_interest_embedding(raw: Any) -> list[float] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        return [float(x) for x in raw]
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            return [float(x) for x in json.loads(text)]
    return None


@router.post("/message", response_model=ChatMessageOut)
@limiter.limit("10/minute")
async def post_chat_message(
    request: Request,
    body: ChatMessageIn,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> ChatMessageOut:
    user_id, jwt = auth
    chat_id = await db.get_or_create_chat(user_id, jwt)
    await db.save_message(chat_id, ChatSender.user, body.content, jwt)
    recent = await db.get_recent_messages(chat_id, jwt, limit=20)
    openai_messages: list[dict[str, str]] = []
    for m in recent:
        role = "user" if m.get("sender") == ChatSender.user.value else "assistant"
        openai_messages.append({"role": role, "content": str(m.get("content", ""))})
    reply = await ai_service.chat_completion(CHAT_SYSTEM_PROMPT, openai_messages)
    await db.save_message(chat_id, ChatSender.assistant, reply, jwt)
    return ChatMessageOut(role=ChatSender.assistant, content=reply)


@router.get("/history", response_model=list[ChatMessageOut])
@limiter.limit("20/minute")
async def get_chat_history(
    request: Request,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> list[ChatMessageOut]:
    """Return the user's recent chat messages (oldest first)."""
    user_id, jwt = auth
    chat_id = await db.get_or_create_chat(user_id, jwt)
    recent = await db.get_recent_messages(chat_id, jwt, limit=50)
    return [
        ChatMessageOut(
            role=ChatSender(str(m.get("sender", "assistant"))),
            content=str(m.get("content", "")),
            created_at=m.get("created_at"),
        )
        for m in recent
    ]


@router.post("/sync")
@limiter.limit("5/minute")
async def post_chat_sync(
    request: Request,
    db: DbServiceDep,
    auth: Annotated[tuple[str, str], Depends(get_current_user_with_token)],
) -> dict[str, str]:
    user_id, jwt = auth
    chat_id = await db.get_or_create_chat(user_id, jwt)
    recent = await db.get_recent_messages(chat_id, jwt, limit=20)
    if not recent:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No messages to sync.",
        )
    corpus = _build_embedding_corpus(recent)
    new_vec = await ai_service.generate_embedding(corpus)
    profile = await db.get_profile(user_id, jwt)
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")
    current = _parse_interest_embedding(profile.get("interest_embedding"))
    if (
        current is not None
        and len(current) == len(new_vec)
        and len(new_vec) == EMBEDDING_DIMENSIONS
    ):
        final_vec = ai_service.update_interest_organically(current, new_vec)
    else:
        final_vec = new_vec
    await db.update_profile_embedding(user_id, final_vec)
    return {"status": "success", "message": "Profile interests updated"}


@router.get("/health")
async def chat_health() -> dict[str, str]:
    return {"status": "ok"}
