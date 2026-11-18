"""One service stack per user.

Everything that holds a user's data (conversations, memory, reminders, mail, plugin
accounts, notifications, audit log) is built once for each account, with that account's id
baked in. Handlers get the stack of the signed-in user (``auth.mine``) and have no way to
name another user's, and every query inside is filtered by that id as well.

What is shared between users is what has to be: the database, the encryption key, the model
endpoint, the MCP server connections, the Telegram bot and the web push keys.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from ..agent.actions import ActionService
from ..agent.builtin_tools import register_builtin_tools
from ..agent.pipeline import Agent, ConversationNotFound
from ..agent.tools import ToolRegistry
from ..audit.service import AuditService, tool_recorder
from ..core.config import AppConfig, ConfigService
from ..core.db import Database
from ..core.events import EventBus
from ..core.models import Attachment, User
from ..core.security import SecretBox
from ..core.settings_store import SettingsStore
from ..llm.client import LLMClient, LLMError, LLMNotConfigured
from ..media.store import AttachmentStore, MediaError, sniff_audio
from ..memory.facts import MemoryService
from ..memory.vectors import VectorStore
from ..notifications.email import EmailChannel, GmailMailer
from ..notifications.service import NotificationService
from ..notifications.telegram import TelegramBot, TelegramChannel
from ..notifications.webpush import VapidKeys, WebPushChannel
from ..plugins.items import ItemStore
from ..plugins.manager import PluginManager
from ..plugins.oauth import OAuthService
from ..proactive.engine import ProactiveEngine
from ..reminders.service import ReminderService
from ..reminders.tools import register_reminder_tools
from ..scheduler.briefing import BriefingService

log = logging.getLogger(__name__)

TELEGRAM_CONVERSATION = "telegram.conversation_id"


@dataclass
class Shared:
    """What every user's stack is built from."""

    config: ConfigService
    db: Database
    box: SecretBox
    settings: SettingsStore
    llm: LLMClient
    tools: ToolRegistry
    vapid: VapidKeys
    bot: TelegramBot
    oauth: OAuthService
    clock: Callable[[], datetime]
    plugin_http: httpx.AsyncClient
    notify_http: httpx.AsyncClient
    mailer: Any = None
    owner_id: int | None = None


@dataclass
class Tenant:
    """One user's services. The fields are filled in by :func:`build`."""

    user_id: int
    username: str
    display_name: str
    role: str
    disabled: bool
    settings: SettingsStore
    shared: Shared
    overrides: dict[str, Any] = field(default_factory=dict)
    _cfg: AppConfig | None = None
    _cfg_key: tuple[int, int] = (-1, -1)
    _overrides_rev: int = 0
    # Wired in build():
    vectors: VectorStore = None  # type: ignore[assignment]
    memory: MemoryService = None  # type: ignore[assignment]
    tools: ToolRegistry = None  # type: ignore[assignment]
    audit: AuditService = None  # type: ignore[assignment]
    actions: ActionService = None  # type: ignore[assignment]
    media: AttachmentStore = None  # type: ignore[assignment]
    agent: Agent = None  # type: ignore[assignment]
    bus: EventBus = None  # type: ignore[assignment]
    items: ItemStore = None  # type: ignore[assignment]
    plugins: PluginManager = None  # type: ignore[assignment]
    notifier: NotificationService = None  # type: ignore[assignment]
    webpush: WebPushChannel = None  # type: ignore[assignment]
    telegram: TelegramChannel = None  # type: ignore[assignment]
    reminders: ReminderService = None  # type: ignore[assignment]
    briefing: BriefingService = None  # type: ignore[assignment]
    proactive: ProactiveEngine = None  # type: ignore[assignment]
    started: bool = False

    # -- configuration ----------------------------------------------------------------

    def cfg(self) -> AppConfig:
        """This user's effective config: server-wide values, the file defaults, then their
        own saved settings. Cached until either layer changes."""
        key = (self.shared.config.version, self._overrides_rev)
        if self._cfg is None or key != self._cfg_key:
            service = self.shared.config
            owner = self.user_id == self.shared.owner_id
            try:
                self._cfg = service.build_for_user(self.overrides, owner=owner)
            except ValueError as e:
                log.error("ignoring saved settings of user %s that no longer validate: %s", self.user_id, e)
                self._cfg = service.build_for_user({}, owner=owner)
            self._cfg_key = key
        return self._cfg

    def preview(self, overrides: dict[str, Any]) -> AppConfig:
        """The config these overrides would produce (raises ValueError when invalid)."""
        return self.shared.config.build_for_user(
            overrides, owner=self.user_id == self.shared.owner_id
        )

    async def save_overrides(self, overrides: dict[str, Any]) -> None:
        await self.settings.save_overrides(overrides)
        self.set_overrides(overrides)

    def set_overrides(self, overrides: dict[str, Any]) -> None:
        self.overrides = dict(overrides)
        self._overrides_rev += 1

    # -- life cycle -------------------------------------------------------------------

    async def start(self) -> None:
        if self.started or self.disabled:
            return
        self.started = True
        await self.plugins.start()

    async def stop(self) -> None:
        if self.started:
            self.started = False
            await self.plugins.stop()
        await self.agent.drain()

    # -- Telegram chat ----------------------------------------------------------------

    async def _telegram_message(self, text: str, images: list[tuple[bytes, str]]) -> str:
        """Chat over Telegram: one ongoing conversation; /new starts another."""
        if text.strip() == "/new" and not images:
            await self.settings.delete(TELEGRAM_CONVERSATION)
            return "Started a new conversation."
        try:
            ids = [(await self.media.save_image(data, name))["id"] for data, name in images]
        except MediaError as e:
            return f"I couldn't use that image: {e}."
        conv_id = await self.settings.get(TELEGRAM_CONVERSATION)
        try:
            try:
                reply = await self.agent.handle(text, conv_id, channel="telegram", images=ids)
            except ConversationNotFound:
                reply = await self.agent.handle(text, None, channel="telegram", images=ids)
        except LLMError as e:
            return f"That didn't work: {e}"
        if reply.conversation_id != conv_id:
            await self.settings.set(TELEGRAM_CONVERSATION, reply.conversation_id)
        return reply.content or "(no reply)"

    async def _telegram_voice(self, audio: bytes) -> str:
        kind = sniff_audio(audio)
        if kind is None:
            raise ValueError("I couldn't read that audio format.")
        try:
            return await self.shared.llm.transcribe(audio, f"voice.{kind[1]}", kind[0])
        except LLMNotConfigured:
            raise ValueError(
                "Voice notes need a speech-to-text model. Ask an admin to set one under Settings."
            ) from None
        except LLMError as e:
            raise ValueError(f"Transcription failed: {e}") from None


def build(shared: Shared, user: User, overrides: dict[str, Any]) -> Tenant:
    """Construct one user's whole stack."""
    uid = user.id
    t = Tenant(
        user_id=uid, username=user.username, display_name=user.display_name, role=user.role,
        disabled=user.disabled, settings=shared.settings.scoped(uid), shared=shared,
        overrides=dict(overrides),
    )
    db, llm, clock = shared.db, shared.llm, shared.clock
    get_config = t.cfg

    t.vectors = VectorStore(db, llm, uid)
    t.memory = MemoryService(db, t.vectors, uid)
    t.tools = ToolRegistry(parent=shared.tools)
    register_builtin_tools(t.tools, t.memory, t.vectors)
    t.audit = AuditService(db, uid, clock)
    t.tools.audit = tool_recorder(t.audit)
    t.actions = ActionService(db, t.tools, t.audit, uid)
    t.media = AttachmentStore(db, get_config, uid)
    t.agent = Agent(
        db, llm, t.memory, t.vectors, t.tools, get_config, t.actions, t.media, user_id=uid
    )
    t.agent.user_label = user.display_name or user.username
    t.bus = EventBus()
    t.items = ItemStore(db, t.vectors, t.bus, lambda: get_config().privacy, uid)
    t.plugins = PluginManager(
        db, shared.box, t.tools, t.bus, t.items, get_config, http=shared.plugin_http,
        user_id=uid, oauth=shared.oauth,
    )

    t.notifier = NotificationService(db, get_config, clock, user_id=uid)
    t.webpush = WebPushChannel(db, shared.box, shared.vapid, shared.notify_http, get_config, uid)
    t.telegram = TelegramChannel(shared.bot, get_config)
    mailer = shared.mailer or GmailMailer(t.plugins, get_config)
    for channel in (t.webpush, t.telegram, EmailChannel(get_config, mailer)):
        t.notifier.register(channel)
    t.notifier.plugin_channels = t.plugins.notification_channels
    t.plugins.notifier = t.notifier

    t.reminders = ReminderService(db, t.notifier, get_config, clock, user_id=uid)
    t.notifier.on_action("reminder", t.reminders.handle_action)
    register_reminder_tools(t.tools, t.reminders, t.notifier, get_config, clock)
    t.briefing = BriefingService(
        t.items, t.reminders, llm, t.notifier, t.settings, get_config, clock
    )
    t.proactive = ProactiveEngine(
        db, t.items, llm, t.reminders, t.tools, t.notifier, t.audit, get_config, clock,
        user_id=uid,
    )
    t.briefing.pending_suggestions = t.proactive.list
    t.bus.subscribe(t.proactive.on_event)
    t.notifier.on_action("suggestion", t.proactive.handle_action)

    t.telegram.on_message = t._telegram_message
    t.telegram.on_voice = t._telegram_voice
    t.telegram.on_action = t.notifier.handle_action
    return t


class Tenants:
    """All the users' stacks, by user id."""

    def __init__(self, shared: Shared):
        self.shared = shared
        self._by_id: dict[int, Tenant] = {}

    def get(self, user_id: int) -> Tenant:
        return self._by_id[user_id]

    def find(self, user_id: int) -> Tenant | None:
        return self._by_id.get(user_id)

    def all(self) -> list[Tenant]:
        return list(self._by_id.values())

    def active(self) -> list[Tenant]:
        return [t for t in self._by_id.values() if not t.disabled]

    async def add(self, user: User, *, start: bool = True) -> Tenant:
        """Build (or rebuild) the stack for an account."""
        if user.id in self._by_id:
            await self.remove(user.id)
        overrides = await self.shared.settings.scoped(user.id).load_overrides()
        tenant = build(self.shared, user, overrides)
        self._by_id[user.id] = tenant
        if start:
            await tenant.start()
        return tenant

    async def remove(self, user_id: int) -> None:
        tenant = self._by_id.pop(user_id, None)
        if tenant is not None:
            await tenant.stop()

    async def sync_account(self, user: User) -> Tenant:
        """Apply a change to the account row (role, name, disabled) to its live stack."""
        tenant = self._by_id[user.id]
        tenant.display_name, tenant.role = user.display_name, user.role
        tenant.agent.user_label = user.display_name or user.username
        if user.disabled and not tenant.disabled:
            tenant.disabled = True
            await tenant.stop()
        elif tenant.disabled and not user.disabled:
            tenant.disabled = False
            await tenant.start()
        return tenant

    async def stop(self) -> None:
        for tenant in self.all():
            await tenant.stop()

    # -- things that span users -------------------------------------------------------

    def telegram_owner(self, chat_id: str) -> TelegramChannel | None:
        """The channel of the one active user who linked this chat (none when it is
        unlinked or, wrongly, linked twice: the chat is then ignored)."""
        found = [
            t.telegram for t in self.active()
            if chat_id and t.cfg().notifications.telegram.chat_id == chat_id
        ]
        return found[0] if len(found) == 1 else None

    def telegram_linking_open(self) -> bool:
        return any(
            t.cfg().notifications.telegram.enabled and not t.cfg().notifications.telegram.chat_id
            for t in self.active()
        )

    def telegram_wanted(self) -> bool:
        return any(t.cfg().notifications.telegram.enabled for t in self.active())

    def chat_taken(self, chat_id: str, except_user: int) -> bool:
        """Whether another account already linked this Telegram chat."""
        return any(
            t.user_id != except_user
            and t.cfg().notifications.telegram.chat_id == chat_id
            for t in self.all()
        )

    async def handle_webhook(
        self, name: str, token: str, headers: dict[str, str], body: bytes
    ) -> bool:
        """Deliver an inbound webhook to whichever user's plugin issued this token."""
        for tenant in self.active():
            if await tenant.plugins.handle_webhook(name, token, headers, body):
                return True
        return False

    async def delete_files(self, user_id: int) -> None:
        """Remove a user's uploaded files from disk (their rows go with the account)."""
        async with self.shared.db.session() as s:
            names = (
                await s.scalars(select(Attachment.stored_name).where(Attachment.user_id == user_id))
            ).all()
        folder: Path = self.shared.config.config.data_path / "attachments"
        for name in names:
            (folder / name).unlink(missing_ok=True)
