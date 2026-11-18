"""REST API: chat, conversations, memory editor, settings."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..agent.pipeline import ConversationNotFound
from ..core.config import (
    AUTO_CATEGORIES,
    PROACTIVE_LEVELS,
    is_secret_path,
    is_server_setting,
    is_user_setting,
)
from ..core.models import Conversation, Message
from ..core.security import mask_secret
from ..core.users import CurrentUser
from ..llm.client import LLMClient, LLMError
from .auth import container, mine, require_admin, require_auth

router = APIRouter(prefix="/api")
authed = APIRouter(dependencies=[Depends(require_auth)])


def _iso(dt: datetime) -> str:
    """Timestamps are stored in UTC; SQLite drops the offset, so put it back."""
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()


@router.get("/health")
async def health():
    return {"ok": True}


# -- chat -----------------------------------------------------------------------------


class ChatBody(BaseModel):
    text: str = Field(default="", max_length=50_000)
    conversation_id: int | None = None
    # Ids returned by POST /api/attachments.
    attachment_ids: list[int] = Field(default_factory=list, max_length=20)


@authed.post("/chat")
async def chat(body: ChatBody, request: Request):
    t = mine(request)
    try:
        reply = await t.agent.handle(
            body.text, body.conversation_id, channel="web", images=body.attachment_ids
        )
    except ConversationNotFound:
        raise HTTPException(404, "Conversation not found") from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    except LLMError as e:
        raise HTTPException(502, f"LLM request failed: {e}") from None
    return {
        "conversation_id": reply.conversation_id,
        "message": {
            "id": reply.message_id,
            "role": "assistant",
            "content": reply.content,
            "meta": {
                "model": reply.model,
                "used_fallback": reply.used_fallback,
                "tool_events": reply.tool_events,
            },
        },
    }


@authed.get("/conversations")
async def list_conversations(request: Request, user: CurrentUser = Depends(require_auth)):
    c = container(request)
    async with c.db.session() as s:
        rows = (
            await s.scalars(
                select(Conversation)
                .where(Conversation.user_id == user.id)
                .order_by(Conversation.updated_at.desc())
                .limit(200)
            )
        ).all()
    return [
        {"id": r.id, "title": r.title or "New conversation", "channel": r.channel,
         "updated_at": _iso(r.updated_at)}
        for r in rows
    ]


@authed.get("/conversations/{conv_id}/messages")
async def list_messages(conv_id: int, request: Request, user: CurrentUser = Depends(require_auth)):
    c = container(request)
    async with c.db.session() as s:
        conv = await s.get(Conversation, conv_id)
        if conv is None or conv.user_id != user.id:
            raise HTTPException(404, "Conversation not found")
        rows = (
            await s.scalars(
                select(Message).where(Message.conversation_id == conv_id).order_by(Message.id)
            )
        ).all()
    return [
        {"id": m.id, "role": m.role, "content": m.content, "meta": m.meta or {},
         "created_at": _iso(m.created_at)}
        for m in rows
    ]


@authed.delete("/conversations/{conv_id}")
async def delete_conversation(
    conv_id: int, request: Request, user: CurrentUser = Depends(require_auth)
):
    c, t = container(request), mine(request)
    async with c.db.session() as s:
        conv = await s.get(Conversation, conv_id)
        if conv is None or conv.user_id != user.id:
            raise HTTPException(404, "Conversation not found")
        ids = (
            await s.scalars(select(Message.id).where(Message.conversation_id == conv_id))
        ).all()
    await t.media.delete_for_messages(list(ids))
    async with c.db.session() as s:
        await s.delete(await s.get(Conversation, conv_id))
        await s.commit()
    await t.vectors.delete("message", [str(i) for i in ids])
    return {"ok": True}


# -- memory ---------------------------------------------------------------------------


class FactBody(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    category: str = Field(default="general", max_length=50)


class FactPatch(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    category: str | None = Field(default=None, max_length=50)


def _fact(f) -> dict[str, Any]:
    return {"id": f.id, "content": f.content, "category": f.category, "source": f.source,
            "updated_at": _iso(f.updated_at)}


@authed.get("/memory/facts")
async def list_facts(request: Request):
    return [_fact(f) for f in await mine(request).memory.list_facts()]


@authed.post("/memory/facts", status_code=201)
async def add_fact(body: FactBody, request: Request):
    return _fact(await mine(request).memory.add_fact(body.content, body.category, "user"))


@authed.patch("/memory/facts/{fact_id}")
async def update_fact(fact_id: int, body: FactPatch, request: Request):
    fact = await mine(request).memory.update_fact(fact_id, body.content, body.category)
    if fact is None:
        raise HTTPException(404, "Fact not found")
    return _fact(fact)


@authed.delete("/memory/facts/{fact_id}")
async def delete_fact(fact_id: int, request: Request):
    if not await mine(request).memory.delete_fact(fact_id):
        raise HTTPException(404, "Fact not found")
    return {"ok": True}


# -- settings -------------------------------------------------------------------------


def _mask(data: Any, path: str = "") -> Any:
    if isinstance(data, dict):
        return {k: _mask(v, f"{path}.{k}" if path else k) for k, v in data.items()}
    return mask_secret(data) if is_secret_path(path) else data


def _settings_view(c, t, admin: bool) -> dict[str, Any]:
    """What the settings page shows: the user's own effective settings, plus the
    server-wide ones for admins."""
    cfg = t.cfg()
    notifications = _mask(cfg.notifications.model_dump(), "notifications")
    server_wide = c.config.config
    if not admin:
        # Shared by everyone: members only need to know whether the bot exists.
        notifications["telegram"].pop("bot_token", None)
        notifications["telegram"].pop("api_base", None)
        notifications["web_push"].pop("contact", None)
    view: dict[str, Any] = {
        "timezone": cfg.timezone,
        "agent": {"name": server_wide.agent.name} if not admin else server_wide.agent.model_dump(),
        "privacy": cfg.privacy.model_dump(),
        "notifications": notifications,
        "briefing": cfg.briefing.model_dump(),
        "proactive": {
            **cfg.proactive.model_dump(),
            # Effective level per category, and which ones may be automatic.
            "levels": {c: cfg.proactive.level(c) for c in PROACTIVE_LEVELS},
            "auto_categories": list(AUTO_CATEGORIES),
        },
        # What the UI may offer: a microphone needs STT, "Listen" needs TTS.
        "capabilities": {"stt": c.llm.configured("stt"), "tts": c.llm.configured("tts")},
        "telegram_bot": bool(server_wide.notifications.telegram.bot_token),
        # Which values come from the settings UI rather than config.yaml / environment.
        "overridden": sorted(
            set(t.overrides) | (set(c.config.overrides) if admin else set())
        ),
        "vector_search": {
            "enabled": t.vectors.enabled,
            "backend": "sqlite-vec" if c.db.vec_available else "in-process",
        },
        "admin": admin,
    }
    if admin:
        view["llm"] = _mask(server_wide.llm.model_dump(), "llm")
    return view


def _merge_values(
    overrides: dict[str, Any], values: dict[str, Any], allowed: Callable[[str], bool]
) -> None:
    """Apply a {dotted.path: value} patch to an override set (null/"" removes)."""
    for path, value in values.items():
        if not allowed(path):
            raise HTTPException(422, f"{path!r} cannot be changed here")
        if is_secret_path(path) and isinstance(value, str) and value.startswith("…"):
            continue  # the masked placeholder was echoed back: leave the secret unchanged
        if value is None or value == "":
            overrides.pop(path, None)
        else:
            overrides[path] = value


class SettingsPatch(BaseModel):
    # dotted path -> new value; null removes the override (reverts to file/env value).
    values: dict[str, Any]


@authed.get("/settings")
async def get_settings(request: Request, user: CurrentUser = Depends(require_auth)):
    return _settings_view(container(request), mine(request), user.is_admin)


@authed.patch("/settings")
async def patch_settings(
    body: SettingsPatch, request: Request, user: CurrentUser = Depends(require_auth)
):
    """Save settings. Personal ones are saved for this user only; server-wide ones (the
    model endpoint, the Telegram bot, ...) need an admin."""
    c, t = container(request), mine(request)
    server_values = {p: v for p, v in body.values.items() if is_server_setting(p)}
    own_values = {p: v for p, v in body.values.items() if not is_server_setting(p)}
    if server_values and not user.is_admin:
        raise HTTPException(403, "Only an admin can change server-wide settings")

    server_overrides, own_overrides = c.config.overrides, dict(t.overrides)
    _merge_values(server_overrides, server_values, is_server_setting)
    _merge_values(own_overrides, own_values, is_user_setting)
    chat = own_values.get("notifications.telegram.chat_id")
    if chat and c.tenants.chat_taken(str(chat).strip(), user.id):
        raise HTTPException(409, "That Telegram chat is linked to another account")
    try:
        c.config.build(server_overrides)
        t.preview(own_overrides)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    if server_values:
        await c.settings.save_overrides(server_overrides)
        c.config.apply_overrides(server_overrides)
    if own_values:
        await t.save_overrides(own_overrides)
        if any(path.startswith("privacy.") for path in own_values):
            # New exclusions apply to data that is already stored, not just future syncs.
            await t.items.apply_privacy()
    return _settings_view(c, t, user.is_admin)


class TestBody(BaseModel):
    slot: str = "chat"
    # Unsaved values to test with, same shape as SettingsPatch.values.
    values: dict[str, Any] = Field(default_factory=dict)


@authed.post("/settings/llm/test")
async def test_llm(body: TestBody, request: Request, _: CurrentUser = Depends(require_admin)):
    c = container(request)
    if body.slot not in ("chat", "fallback", "vision", "stt", "tts", "embeddings"):
        raise HTTPException(422, "unknown slot")
    if not body.values:
        return await c.llm.test_connection(body.slot)
    overrides = c.config.overrides
    _merge_values(overrides, body.values, lambda p: p.startswith("llm."))
    try:
        preview = c.config.build(overrides)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    probe = LLMClient(lambda: preview.llm, http_client=c.llm._http_client)
    try:
        return await probe.test_connection(body.slot)
    finally:
        await probe.aclose()


router.include_router(authed)
