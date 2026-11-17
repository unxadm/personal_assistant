"""Email channel: plain-text mail sent through the Gmail API by the Gmail plugin.

There is no SMTP here and no mail password to store: the message goes out from the Google
account that is already connected, using its OAuth token. Sending is a separate, opt-in
permission on the plugin (the ``gmail.send`` scope). Only this channel uses it; the chat
model has no tool that sends mail.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol

from ..core.config import AppConfig
from ..plugins.manager import PluginManager
from ..plugins.sdk import Notification, PluginError
from .service import Channel, ChannelError, absolute_link

PLUGIN = "gmail"


class Mailer(Protocol):
    def problem(self) -> str | None:
        """Why mail cannot be sent right now, or None when it can."""

    async def send(self, to: str | None, subject: str, body: str) -> None:
        """Send one plain-text message; ``to=None`` means the account's own address."""


class GmailMailer:
    def __init__(self, plugins: PluginManager, get_config: Callable[[], AppConfig]):
        self._plugins = plugins
        self._get_config = get_config

    def problem(self) -> str | None:
        record = self._plugins.records.get(PLUGIN)
        if record is None:
            return "the Gmail plugin is not installed"
        if not record.enabled:
            return "turn on the Gmail plugin and connect your account (Plugins page)"
        if not record.effective_config.get("enable_send"):
            return "turn on “Allow sending notification email” in the Gmail plugin, then reconnect"
        if record.status == "needs_auth":
            return "reconnect Gmail on the Plugins page to grant permission to send"
        if record.status != "running" or record.instance is None:
            return f"the Gmail plugin is not running ({record.status})"
        return None

    async def send(self, to: str | None, subject: str, body: str) -> None:
        problem = self.problem()
        if problem:
            raise PluginError(problem)
        instance = self._plugins.records[PLUGIN].instance
        await asyncio.wait_for(
            instance.send_notification(to, subject, body),  # type: ignore[union-attr]
            self._get_config().plugins.call_timeout,
        )


class EmailChannel(Channel):
    name = "email"
    label = "Email"

    def __init__(self, get_config: Callable[[], AppConfig], mailer: Mailer):
        self._get_config = get_config
        self._mailer = mailer

    async def status(self) -> dict[str, Any]:
        cfg = self._get_config().notifications.email
        if not cfg.enabled:
            return {"ready": False, "detail": "turned off"}
        problem = self._mailer.problem()
        if problem:
            return {"ready": False, "detail": problem}
        return {"ready": True, "detail": f"to {cfg.to or 'your own Gmail address'}, sent through Gmail"}

    async def send(self, notification: Notification) -> None:
        app = self._get_config()
        subject = f"[{app.agent.name}] {notification.title}"[:200].replace("\n", " ")
        lines = [notification.title, "", notification.body]
        link = absolute_link(notification.url, app.server.public_url) or app.server.public_url
        if link:
            lines += ["", link]
        try:
            await self._mailer.send(app.notifications.email.to or None, subject, "\n".join(lines).strip() + "\n")
        except TimeoutError as e:
            raise ChannelError("Gmail did not answer in time") from e
        except PluginError as e:
            raise ChannelError(f"Gmail: {e}") from e
