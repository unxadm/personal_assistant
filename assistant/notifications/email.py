"""Email channel: plain-text mail over SMTP (stdlib, run in a worker thread)."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any

from ..core.config import AppConfig, EmailConfig
from ..plugins.sdk import Notification
from .service import Channel, ChannelError


def smtp_send(cfg: EmailConfig, message: EmailMessage) -> None:
    assert cfg.host
    context = ssl.create_default_context()
    if cfg.security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(cfg.host, cfg.port, timeout=30)
    with server:
        if cfg.security == "starttls":
            server.starttls(context=context)
        if cfg.username:
            server.login(cfg.username, cfg.password or "")
        server.send_message(message)


class EmailChannel(Channel):
    name = "email"
    label = "Email"

    def __init__(
        self,
        get_config: Callable[[], AppConfig],
        sender: Callable[[EmailConfig, EmailMessage], None] = smtp_send,
    ):
        self._get_config = get_config
        self._sender = sender

    async def status(self) -> dict[str, Any]:
        cfg = self._get_config().notifications.email
        if not cfg.enabled:
            return {"ready": False, "detail": "turned off"}
        missing = [k for k in ("host", "to") if not getattr(cfg, k)]
        if missing:
            return {"ready": False, "detail": "missing: " + ", ".join(missing)}
        return {"ready": True, "detail": f"to {cfg.to} via {cfg.host}"}

    async def send(self, notification: Notification) -> None:
        app = self._get_config()
        cfg = app.notifications.email
        message = EmailMessage()
        message["Subject"] = f"[{app.agent.name}] {notification.title}"[:200].replace("\n", " ")
        message["From"] = cfg.from_addr or cfg.username or cfg.to
        message["To"] = cfg.to
        lines = [notification.title, "", notification.body]
        link = notification.url or app.server.public_url
        if link:
            lines += ["", link]
        message.set_content("\n".join(lines).strip() + "\n")
        try:
            await asyncio.to_thread(self._sender, cfg, message)
        except (smtplib.SMTPException, OSError) as e:
            # SMTP errors can echo the login; report the kind only.
            raise ChannelError(f"SMTP {type(e).__name__}") from e
