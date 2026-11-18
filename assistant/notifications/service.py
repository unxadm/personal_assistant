"""Notification dispatcher: routing by category, quiet hours, retries, and the log.

Every notification is stored first, so it shows up in the app even when no channel is
configured or every channel is down.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import delete, select

from ..agent.untrusted import safe_url
from ..core.config import AppConfig
from ..core.db import Database
from ..core.models import NotificationRecord, utcnow
from ..plugins.sdk import Notification
from ..reminders.recurrence import aware

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(minutes=1)
KEEP = timedelta(days=30)


def absolute_link(url: str | None, public_url: str | None) -> str | None:
    """A link usable outside the web app (Telegram, email). In-app links such as
    ``/?tab=briefing`` need ``server.public_url`` in front; without it there is no link."""
    if url and url.startswith("/"):
        return public_url.rstrip("/") + url if public_url else None
    return url


class ChannelError(Exception):
    """Delivery failed; the message is safe to show to the user."""


class Channel:
    """A built-in delivery channel."""

    name = ""
    label = ""

    async def status(self) -> dict[str, Any]:
        """``{"ready": bool, "detail": str}`` — ready means a send can be attempted."""
        raise NotImplementedError

    async def send(self, notification: Notification) -> None:
        raise NotImplementedError


ActionHandler = Callable[[str, str], Awaitable[str]]


class NotificationService:
    """One user's notifications: their inbox and log, sent through their own channels."""

    def __init__(
        self,
        db: Database,
        get_config: Callable[[], AppConfig],
        clock: Callable[[], datetime] = utcnow,
        *,
        user_id: int,
    ):
        self._user_id = user_id
        self._db = db
        self._get_config = get_config
        self._clock = clock
        self.channels: dict[str, Channel] = {}
        # Notification-channel plugins: name -> send coroutine. Set by the app.
        self.plugin_channels: Callable[[], dict[str, Callable[[Notification], Awaitable[None]]]] = dict
        self._actions: dict[str, ActionHandler] = {}

    def register(self, channel: Channel) -> None:
        self.channels[channel.name] = channel

    def on_action(self, ref_kind: str, handler: ActionHandler) -> None:
        """Handle button presses for refs like ``<ref_kind>:<id>``."""
        self._actions[ref_kind] = handler

    async def handle_action(self, ref: str, action: str) -> str:
        kind, _, ident = ref.partition(":")
        handler = self._actions.get(kind)
        if handler is None or not ident:
            return "That button no longer works."
        return await handler(ident, action)

    # -- quiet hours ------------------------------------------------------------------

    def quiet_until(self, now: datetime) -> datetime | None:
        """End of the current quiet period, or None when it is not quiet now."""
        cfg = self._get_config()
        quiet = cfg.notifications.quiet_hours
        if not quiet.enabled or quiet.start == quiet.end:
            return None
        local = aware(now).astimezone(cfg.tz)
        start, end = time.fromisoformat(quiet.start), time.fromisoformat(quiet.end)
        clock = local.time().replace(second=0, microsecond=0)
        inside = start <= clock < end if start < end else clock >= start or clock < end
        if not inside:
            return None
        finish = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
        if finish <= local:
            finish += timedelta(days=1)
        return finish.astimezone(now.tzinfo)

    # -- sending ----------------------------------------------------------------------

    async def notify(
        self,
        *,
        category: str,
        title: str,
        body: str = "",
        url: str | None = None,
        urgency: str = "normal",
        ref: str | None = None,
        actions: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        now = self._clock()
        hold_until = None if urgency == "high" else self.quiet_until(now)
        row = NotificationRecord(
            user_id=self._user_id, category=category, title=title, body=body, url=safe_url(url), urgency=urgency, ref=ref,
            status="held", deliver_after=hold_until or now,
            results={"_actions": actions or []},
        )
        async with self._db.session() as s:
            s.add(row)
            await s.commit()
        if hold_until is None:
            await self._deliver(row.id, now)
        return await self.get(row.id)

    async def flush(self, now: datetime | None = None) -> int:
        """Deliver held notifications whose time has come (quiet hours over, retries)."""
        now = now or self._clock()
        async with self._db.session() as s:
            ids = (
                await s.scalars(
                    select(NotificationRecord.id)
                    .where(
                        NotificationRecord.user_id == self._user_id,
                        NotificationRecord.status == "held",
                        NotificationRecord.deliver_after <= now,
                    )
                    .order_by(NotificationRecord.id)
                )
            ).all()
            await s.execute(
                delete(NotificationRecord).where(
                    NotificationRecord.user_id == self._user_id,
                    NotificationRecord.created_at < now - KEEP,
                )
            )
            await s.commit()
        for record_id in ids:
            await self._deliver(record_id, now)
        return len(ids)

    async def _ready(self) -> dict[str, Callable[[Notification], Awaitable[None]]]:
        ready: dict[str, Callable[[Notification], Awaitable[None]]] = {}
        for name, channel in self.channels.items():
            try:
                if (await channel.status())["ready"]:
                    ready[name] = channel.send
            except Exception:
                log.exception("status check failed for channel %s", name)
        ready.update(self.plugin_channels())
        return ready

    async def _deliver(self, record_id: int, now: datetime) -> None:
        cfg = self._get_config().notifications
        async with self._db.session() as s:
            row = await s.get(NotificationRecord, record_id)
            if row is None or row.status != "held":
                return
            results = dict(row.results or {})
            notification = Notification(
                id=row.id, title=row.title, body=row.body, url=row.url, urgency=row.urgency,
                category=row.category, ref=row.ref, actions=results.get("_actions", []),
            )

        ready = await self._ready()
        wanted = cfg.categories.get(notification.category, cfg.default_channels)
        first = [name for name in wanted if name in ready]
        # If none of the preferred channels works, try the others rather than drop it.
        rest = [name for name in ready if name not in first]
        delivered = False
        for name in first + rest:
            if delivered and name in rest:
                break  # fallbacks are tried one at a time, only until one works
            if results.get(name) == "ok":
                delivered = True
                continue
            try:
                await ready[name](notification)
                results[name], delivered = "ok", True
            except Exception as e:
                detail = str(e) if isinstance(e, ChannelError) else type(e).__name__
                results[name] = f"error: {detail}"[:300]
                log.warning("notification %s via %s failed: %s", record_id, name, detail)

        attempts = int(results.get("_attempts", 0)) + 1
        results["_attempts"] = attempts
        async with self._db.session() as s:
            row = await s.get(NotificationRecord, record_id)
            if row is None:
                return
            row.results = results
            if delivered:
                row.status, row.sent_at = "sent", now
            elif ready and attempts < MAX_ATTEMPTS:
                row.deliver_after = now + RETRY_DELAY * attempts
            else:
                row.status = "failed"
            await s.commit()

    # -- views ------------------------------------------------------------------------

    @staticmethod
    def view(row: NotificationRecord) -> dict[str, Any]:
        results = row.results or {}
        return {
            "id": row.id,
            "category": row.category,
            "title": row.title,
            "body": row.body,
            "url": row.url,
            "urgency": row.urgency,
            "ref": row.ref,
            "status": row.status,
            "deliver_after": aware(row.deliver_after).isoformat() if row.deliver_after else None,
            "channels": {k: v for k, v in results.items() if not k.startswith("_")},
            "created_at": aware(row.created_at).isoformat(),
            "sent_at": aware(row.sent_at).isoformat() if row.sent_at else None,
        }

    async def get(self, record_id: int) -> dict[str, Any]:
        async with self._db.session() as s:
            row = await s.get(NotificationRecord, record_id)
            assert row is not None and row.user_id == self._user_id
            return self.view(row)

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self._db.session() as s:
            rows = (
                await s.scalars(
                    select(NotificationRecord)
                    .where(NotificationRecord.user_id == self._user_id)
                    .order_by(NotificationRecord.id.desc())
                    .limit(limit)
                )
            ).all()
        return [self.view(r) for r in rows]

    async def channel_status(self) -> list[dict[str, Any]]:
        out = []
        for name, channel in self.channels.items():
            try:
                status = await channel.status()
            except Exception as e:
                status = {"ready": False, "detail": type(e).__name__}
            out.append({"name": name, "label": channel.label, "plugin": False, **status})
        for name in self.plugin_channels():
            out.append({"name": name, "label": name, "plugin": True, "ready": True, "detail": "plugin"})
        return out

    async def test(self, channel: str) -> None:
        """Send a test message through one channel; raises ChannelError on failure."""
        ready = await self._ready()
        if channel not in ready:
            raise ChannelError("this channel is not set up yet")
        try:
            await ready[channel](
                Notification(title="Test notification", body="Notifications are working.")
            )
        except ChannelError:
            raise
        except Exception as e:
            raise ChannelError(type(e).__name__) from e
