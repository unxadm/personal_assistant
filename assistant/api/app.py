"""FastAPI application factory and service wiring."""

from __future__ import annotations

import asyncio
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

from ..agent.tools import ToolRegistry
from ..core.config import LEGACY_EMAIL_KEYS, ConfigService, is_server_setting
from ..core.db import Database
from ..core.models import utcnow
from ..core.security import SecretBox, restrict_permissions, setup_logging
from ..core.settings_store import SettingsStore
from ..core.users import UserService
from ..llm.client import LLMClient
from ..mcpclient.manager import McpManager
from ..notifications.telegram import TelegramBot
from ..notifications.webpush import VapidKeys
from ..plugins.oauth import OAuthService
from ..scheduler.service import Scheduler
from . import (
    auth,
    hardening,
    media_routes,
    plugin_routes,
    proactive_routes,
    reminder_routes,
    routes,
    user_routes,
)
from .tenants import Shared, Tenant, Tenants

log = logging.getLogger(__name__)

WEB_DIST = Path(__file__).resolve().parent.parent.parent / "web" / "dist"


async def _server_overrides(settings: SettingsStore) -> tuple[dict, list[str]]:
    """Saved server-wide settings, and the saved paths that are not server-wide (left over
    from before settings became personal) and so are ignored here."""
    saved = await settings.load_overrides()
    keep = {path: value for path, value in saved.items() if is_server_setting(path)}
    return keep, sorted(set(saved) - set(keep))


async def _drop_stale_email_settings(tenant: Tenant) -> None:
    stale = [k for k in tenant.overrides if k.removeprefix("notifications.email.") in LEGACY_EMAIL_KEYS]
    if stale:
        # Email no longer uses SMTP: forget the saved server settings and password.
        await tenant.save_overrides({k: v for k, v in tenant.overrides.items() if k not in stale})
        log.info("removed %d saved SMTP settings; email is now sent through Gmail", len(stale))


@dataclass
class Container:
    """Everything the request handlers need. What belongs to one user is not here: it lives
    in that user's :class:`Tenant` (``c.of(user_id)``)."""

    config: ConfigService
    db: Database
    box: SecretBox
    settings: SettingsStore  # server-wide; a user's own is ``c.of(id).settings``
    llm: LLMClient
    tools: ToolRegistry  # server-wide tools (MCP); users have their own, chained to it
    login_guard: auth.LoginGuard
    users: UserService
    tenants: Tenants
    oauth: OAuthService
    vapid: VapidKeys
    telegram: TelegramBot
    scheduler: Scheduler
    clock: Callable[[], datetime]
    mcp: McpManager
    # One-time code the first-run form needs (None: not needed).
    setup_code: str | None = None

    def of(self, user_id: int) -> Tenant:
        return self.tenants.get(user_id)


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
        users = UserService(db, lambda: config.config.auth.min_password_length)

        overrides, ignored = await _server_overrides(settings)
        if ignored:
            log.warning("ignoring server-wide saved settings that are personal now: %s", ", ".join(ignored))
        try:
            config.apply_overrides(overrides)
        except ValueError as e:
            log.error("ignoring stored settings overrides that no longer validate: %s", e)

        llm = LLMClient(lambda: config.config.llm, http_client=llm_http_client)
        tools = ToolRegistry()
        mcp = McpManager(tools, lambda: config.config)

        def get_config():
            return config.config

        plugin_http = plugin_http_client or httpx.AsyncClient()
        notify_http = notify_http_client or httpx.AsyncClient()
        bot = TelegramBot(notify_http, get_config)
        shared = Shared(
            config=config, db=db, box=box, settings=settings, llm=llm, tools=tools,
            vapid=VapidKeys(settings), bot=bot, oauth=OAuthService(plugin_http), clock=clock,
            plugin_http=plugin_http, notify_http=notify_http, mailer=mailer,
            owner_id=await users.owner_id(),
        )
        tenants = Tenants(shared)
        bot.resolve = tenants.telegram_owner
        bot.linking_open = tenants.telegram_linking_open
        bot.wanted = tenants.telegram_wanted

        def each_user(job):
            """A scheduler job that runs once for every active user; one user's failure
            never stops the others."""

            async def run(now: datetime) -> None:
                for tenant in tenants.active():
                    try:
                        await job(tenant, now)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("scheduled job failed for user %s", tenant.user_id)

            return run

        scheduler = Scheduler(get_config, clock)
        scheduler.add_job("reminders", each_user(lambda t, now: t.reminders.fire_due(now)))
        scheduler.add_job("notifications", each_user(lambda t, now: t.notifier.flush(now)))
        scheduler.add_job("briefing", each_user(lambda t, now: t.briefing.maybe_run(now)))
        scheduler.add_job("attachments", each_user(lambda t, now: t.media.cleanup(now)))
        scheduler.add_job("proactive", each_user(lambda t, now: t.proactive.tick(now)))

        app.state.c = Container(
            config=config, db=db, box=box, settings=settings, llm=llm, tools=tools,
            login_guard=auth.LoginGuard(), users=users, tenants=tenants, oauth=shared.oauth,
            vapid=shared.vapid, telegram=bot, scheduler=scheduler, clock=clock, mcp=mcp,
        )
        for user in await users.list():
            tenant = await tenants.add(user, start=False)
            await _drop_stale_email_settings(tenant)
        if await users.count() == 0:
            app.state.c.setup_code = auth.new_setup_code(cfg.server.host)
            if app.state.c.setup_code:
                log.warning(
                    "No account exists yet. Open the app and enter this one-time setup code: %s",
                    app.state.c.setup_code,
                )
        for tenant in tenants.active():
            await tenant.start()
        await mcp.start()
        if cfg.scheduler.enabled:
            scheduler.start()
            bot.start()
        try:
            yield
        finally:
            await scheduler.stop()
            await bot.stop()
            await mcp.stop()
            await tenants.stop()
            if plugin_http_client is None:
                await plugin_http.aclose()
            if notify_http_client is None:
                await notify_http.aclose()
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
    app.include_router(user_routes.router)

    if WEB_DIST.is_dir():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            candidate = (WEB_DIST / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(WEB_DIST):
                return FileResponse(candidate)
            return FileResponse(WEB_DIST / "index.html")

    return app
