"""FastAPI application factory and service wiring."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..agent.actions import ActionService
from ..agent.builtin_tools import register_builtin_tools
from ..agent.pipeline import Agent, ConversationNotFound
from ..agent.tools import ToolRegistry
from ..audit.service import AuditService, tool_recorder
from ..core.config import LEGACY_EMAIL_KEYS, ConfigService
from ..core.db import Database
from ..core.events import EventBus
from ..core.models import utcnow
from ..core.security import SecretBox, restrict_permissions, setup_logging
from ..core.settings_store import SettingsStore
from ..llm.client import LLMClient, LLMError, LLMNotConfigured
from ..mcpclient.manager import McpManager
from ..media.store import AttachmentStore, MediaError, sniff_audio
from ..memory.facts import MemoryService
from ..memory.vectors import VectorStore
from ..notifications.email import EmailChannel, GmailMailer
from ..notifications.service import NotificationService
from ..notifications.telegram import TelegramChannel
from ..notifications.webpush import WebPushChannel
from ..plugins.items import ItemStore
from ..plugins.manager import PluginManager
from ..proactive.engine import ProactiveEngine
from ..reminders.service import ReminderService
from ..reminders.tools import register_reminder_tools
from ..scheduler.briefing import BriefingService
from ..scheduler.service import Scheduler
from . import (
    auth,
    hardening,
    media_routes,
    plugin_routes,
    proactive_routes,
    reminder_routes,
    routes,
)

log = logging.getLogger(__name__)

WEB_DIST = Path(__file__).resolve().parent.parent.parent / "web" / "dist"


@dataclass
class Container:
    """Everything the request handlers need."""

    config: ConfigService
    db: Database
    box: SecretBox
    settings: SettingsStore
    llm: LLMClient
    vectors: VectorStore
    memory: MemoryService
    tools: ToolRegistry
    agent: Agent
    login_guard: auth.LoginGuard
    bus: EventBus
    items: ItemStore
    actions: ActionService
    plugins: PluginManager
    notifier: NotificationService
    webpush: WebPushChannel
    telegram: TelegramChannel
    reminders: ReminderService
    briefing: BriefingService
    scheduler: Scheduler
    clock: Callable[[], datetime]
    media: AttachmentStore
    audit: AuditService
    proactive: ProactiveEngine
    mcp: McpManager
    # One-time code the first-run form needs (None: not needed).
    setup_code: str | None = None


def create_app(
    config: ConfigService | None = None,
    *,
    llm_http_client: httpx.AsyncClient | None = None,
    plugin_http_client: httpx.AsyncClient | None = None,
    notify_http_client: httpx.AsyncClient | None = None,
    mailer=None,
    clock: Callable[[], datetime] = utcnow,
    migrate: bool = True,
    secret_key: str | None = None,
) -> FastAPI:
    """Build the app. The ``*_http_client``, ``mailer`` and ``clock`` arguments let
    tests route outside traffic to fakes and control time."""
    config = config or ConfigService.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = config.config
        setup_logging(cfg.log_level)
        db = Database(cfg.database_url)
        if migrate:
            await db.migrate()
        else:
            await db.create_all()
        box = SecretBox.from_env_or_file(cfg.data_path, secret_key)
        restrict_permissions(cfg.data_path)
        settings = SettingsStore(db, box)
        overrides = await settings.load_overrides()
        stale = [k for k in overrides if k.removeprefix("notifications.email.") in LEGACY_EMAIL_KEYS]
        if stale:
            # Email no longer uses SMTP: forget the saved server settings and password.
            overrides = {k: v for k, v in overrides.items() if k not in stale}
            await settings.save_overrides(overrides)
            log.info("removed %d saved SMTP settings; email is now sent through Gmail", len(stale))
        try:
            config.apply_overrides(overrides)
        except ValueError as e:
            log.error("ignoring stored settings overrides that no longer validate: %s", e)

        llm = LLMClient(lambda: config.config.llm, http_client=llm_http_client)
        vectors = VectorStore(db, llm)
        memory = MemoryService(db, vectors)
        tools = ToolRegistry()
        register_builtin_tools(tools, memory, vectors)
        audit = AuditService(db, clock)
        tools.audit = tool_recorder(audit)
        actions = ActionService(db, tools, audit)
        media = AttachmentStore(db, lambda: config.config)
        agent = Agent(db, llm, memory, vectors, tools, lambda: config.config, actions, media)
        bus = EventBus()
        items = ItemStore(db, vectors, bus, lambda: config.config.privacy)
        plugins = PluginManager(
            db, box, tools, bus, items, lambda: config.config, http=plugin_http_client
        )
        mcp = McpManager(tools, lambda: config.config)

        def get_config():
            return config.config

        notify_http = notify_http_client or httpx.AsyncClient()
        notifier = NotificationService(db, get_config, clock)
        webpush = WebPushChannel(db, box, settings, notify_http, get_config)
        telegram = TelegramChannel(notify_http, get_config)
        for channel in (webpush, telegram, EmailChannel(get_config, mailer or GmailMailer(plugins, get_config))):
            notifier.register(channel)
        notifier.plugin_channels = plugins.notification_channels
        plugins.notifier = notifier

        reminders = ReminderService(db, notifier, get_config, clock)
        notifier.on_action("reminder", reminders.handle_action)
        register_reminder_tools(tools, reminders, notifier, get_config, clock)
        briefing = BriefingService(items, reminders, llm, notifier, settings, get_config, clock)
        proactive = ProactiveEngine(
            db, items, llm, reminders, tools, notifier, audit, get_config, clock
        )
        briefing.pending_suggestions = proactive.list
        bus.subscribe(proactive.on_event)
        notifier.on_action("suggestion", proactive.handle_action)

        async def telegram_message(text: str, images: list[tuple[bytes, str]]) -> str:
            """Chat over Telegram: one ongoing conversation; /new starts another."""
            key = "telegram.conversation_id"
            if text.strip() == "/new" and not images:
                await settings.delete(key)
                return "Started a new conversation."
            try:
                ids = [(await media.save_image(data, name))["id"] for data, name in images]
            except MediaError as e:
                return f"I couldn't use that image: {e}."
            conv_id = await settings.get(key)
            try:
                try:
                    reply = await agent.handle(text, conv_id, channel="telegram", images=ids)
                except ConversationNotFound:
                    reply = await agent.handle(text, None, channel="telegram", images=ids)
            except LLMError as e:
                return f"That didn't work: {e}"
            if reply.conversation_id != conv_id:
                await settings.set(key, reply.conversation_id)
            return reply.content or "(no reply)"

        async def telegram_voice(audio: bytes) -> str:
            kind = sniff_audio(audio)
            if kind is None:
                raise ValueError("I couldn't read that audio format.")
            try:
                return await llm.transcribe(audio, f"voice.{kind[1]}", kind[0])
            except LLMNotConfigured:
                raise ValueError(
                    "Voice notes need a speech-to-text model. Set one under Settings."
                ) from None
            except LLMError as e:
                raise ValueError(f"Transcription failed: {e}") from None

        telegram.on_message = telegram_message
        telegram.on_voice = telegram_voice
        telegram.on_action = notifier.handle_action

        scheduler = Scheduler(get_config, clock)
        scheduler.add_job("reminders", reminders.fire_due)
        scheduler.add_job("notifications", notifier.flush)
        scheduler.add_job("briefing", briefing.maybe_run)
        scheduler.add_job("attachments", media.cleanup)
        scheduler.add_job("proactive", proactive.tick)

        app.state.c = Container(
            config, db, box, settings, llm, vectors, memory, tools, agent, auth.LoginGuard(),
            bus, items, actions, plugins, notifier, webpush, telegram, reminders, briefing,
            scheduler, clock, media, audit, proactive, mcp,
        )
        if await settings.get(auth.PASSWORD_KEY) is None:
            app.state.c.setup_code = auth.new_setup_code(cfg.server.host)
            if app.state.c.setup_code:
                log.warning(
                    "No password is set yet. Open the app and enter this one-time setup code: %s",
                    app.state.c.setup_code,
                )
        await plugins.start()
        await mcp.start()
        if cfg.scheduler.enabled:
            scheduler.start()
            telegram.start()
        try:
            yield
        finally:
            await scheduler.stop()
            await telegram.stop()
            await mcp.stop()
            await plugins.stop()
            if notify_http_client is None:
                await notify_http.aclose()
            await agent.drain()
            await llm.aclose()
            await db.dispose()

    app = FastAPI(title="Personal Assistant", lifespan=lifespan, docs_url=None, redoc_url=None)
    hardening.install(app)
    app.include_router(auth.router)
    app.include_router(routes.router)
    app.include_router(plugin_routes.router)
    app.include_router(reminder_routes.router)
    app.include_router(media_routes.router)
    app.include_router(proactive_routes.router)

    if WEB_DIST.is_dir():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            candidate = (WEB_DIST / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(WEB_DIST):
                return FileResponse(candidate)
            return FileResponse(WEB_DIST / "index.html")

    return app
