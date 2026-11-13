"""Reminders: storage, state transitions, and firing.

States: ``scheduled`` -> (fires) -> ``fired`` -> ``done``, or back to ``scheduled`` when
snoozed. A recurring reminder goes straight back to ``scheduled`` for its next occurrence
and only reaches ``fired`` when the series has ended.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from ..core.config import AppConfig
from ..core.db import Database
from ..core.models import Reminder, utcnow
from .recurrence import (
    aware,
    describe_rrule,
    format_local,
    local_naive,
    next_occurrence,
    normalize_rrule,
    parse_when,
)

log = logging.getLogger(__name__)

URGENCIES = ("low", "normal", "high")
# A one-off reminder may be this far in the past when created ("now" is fine).
PAST_GRACE = timedelta(minutes=2)
# Fired later than this (server was down, quiet hours) and the notification says so.
LATE_AFTER = timedelta(minutes=5)
MAX_SNOOZE = timedelta(days=366)


class ReminderError(ValueError):
    """Bad input or an operation that does not fit the reminder's state."""


class ReminderNotFound(ReminderError):
    pass


class ReminderService:
    def __init__(
        self,
        db: Database,
        notifier: Any,  # NotificationService
        get_config: Callable[[], AppConfig],
        clock: Callable[[], datetime] = utcnow,
    ):
        self._db = db
        self._notifier = notifier
        self._get_config = get_config
        self._clock = clock

    # -- views ------------------------------------------------------------------------

    def view(self, r: Reminder) -> dict[str, Any]:
        tz = self._get_config().tz
        due, nxt = aware(r.due_at), aware(r.next_at) if r.next_at else None
        snoozed = r.status == "scheduled" and nxt is not None and nxt != due
        return {
            "id": r.id,
            "text": r.text,
            "due_at": due.isoformat(),
            "due_local": due.astimezone(tz).strftime("%Y-%m-%dT%H:%M"),
            "next_at": nxt.isoformat() if nxt else None,
            "snoozed_until": nxt.isoformat() if snoozed else None,
            "rrule": r.rrule,
            "repeat": describe_rrule(r.rrule),
            "status": r.status,
            "urgency": r.urgency,
            "source": r.source,
            "last_fired_at": aware(r.last_fired_at).isoformat() if r.last_fired_at else None,
        }

    async def list(self, *, include_closed: bool = False, limit: int = 200) -> list[dict[str, Any]]:
        stmt = select(Reminder)
        if not include_closed:
            stmt = stmt.where(Reminder.status.in_(("scheduled", "fired")))
        stmt = stmt.order_by(Reminder.due_at).limit(limit)
        async with self._db.session() as s:
            return [self.view(r) for r in (await s.scalars(stmt)).all()]

    async def get(self, reminder_id: int) -> dict[str, Any]:
        async with self._db.session() as s:
            return self.view(await self._load(s, reminder_id))

    @staticmethod
    async def _load(s, reminder_id: int) -> Reminder:
        row = await s.get(Reminder, reminder_id)
        if row is None:
            raise ReminderNotFound(f"no reminder with id {reminder_id}")
        return row

    # -- create / change --------------------------------------------------------------

    def _schedule(
        self, when: str | datetime, rrule: str | None, now: datetime
    ) -> tuple[datetime, str | None, str]:
        """Resolve user input to (first due time, canonical rrule, local anchor)."""
        tz = self._get_config().tz
        try:
            start = parse_when(when, tz)
            rule = normalize_rrule(rrule, tz) if rrule else None
        except ReminderError:
            raise
        except ValueError as e:
            raise ReminderError(str(e)) from None
        anchor = local_naive(start, tz)
        if rule is None:
            if start < now - PAST_GRACE:
                raise ReminderError(
                    f"{format_local(start, tz)} is in the past (it is now {format_local(now, tz)})"
                )
            return start, None, anchor.isoformat()
        # The rule decides the real first occurrence (e.g. BYDAY=MO when `when` is a Tuesday).
        due = next_occurrence(anchor, rule, tz, start, inclusive=True)
        if due is not None and due < now - PAST_GRACE:
            due = next_occurrence(anchor, rule, tz, now)
        if due is None:
            raise ReminderError("that recurrence has no occurrences in the future")
        return due, rule, anchor.isoformat()

    async def create(
        self,
        text: str,
        when: str | datetime,
        *,
        rrule: str | None = None,
        urgency: str = "normal",
        source: str = "user",
    ) -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise ReminderError("reminder text is empty")
        if urgency not in URGENCIES:
            raise ReminderError(f"urgency must be one of {', '.join(URGENCIES)}")
        due, rule, anchor = self._schedule(when, rrule, self._clock())
        row = Reminder(
            text=text[:2000], due_at=due, next_at=due, rrule=rule, anchor=anchor,
            timezone=self._get_config().timezone, urgency=urgency, source=source,
        )
        async with self._db.session() as s:
            s.add(row)
            await s.commit()
        return self.view(row)

    async def update(
        self,
        reminder_id: int,
        *,
        text: str | None = None,
        when: str | datetime | None = None,
        rrule: str | None = None,
        urgency: str | None = None,
    ) -> dict[str, Any]:
        """Change a reminder. ``rrule=""`` removes the recurrence. Changing the time or
        recurrence re-schedules it (also re-opens a finished one)."""
        async with self._db.session() as s:
            row = await self._load(s, reminder_id)
            if text is not None:
                if not text.strip():
                    raise ReminderError("reminder text is empty")
                row.text = text.strip()[:2000]
            if urgency is not None:
                if urgency not in URGENCIES:
                    raise ReminderError(f"urgency must be one of {', '.join(URGENCIES)}")
                row.urgency = urgency
            if when is not None or rrule is not None:
                new_rule = row.rrule if rrule is None else (rrule or None)
                start = when if when is not None else aware(row.due_at)
                row.due_at, row.rrule, row.anchor = self._schedule(start, new_rule, self._clock())
                row.next_at, row.status = row.due_at, "scheduled"
                row.timezone = self._get_config().timezone
            row.updated_at = utcnow()
            await s.commit()
            return self.view(row)

    async def snooze(
        self, reminder_id: int, *, minutes: int | None = None, until: str | datetime | None = None
    ) -> dict[str, Any]:
        cfg, now = self._get_config(), self._clock()
        if until is not None:
            try:
                target = parse_when(until, cfg.tz)
            except ValueError as e:
                raise ReminderError(str(e)) from None
        else:
            target = now + timedelta(minutes=minutes or cfg.notifications.default_snooze_minutes)
        if target <= now:
            raise ReminderError("snooze time is in the past")
        if target - now > MAX_SNOOZE:
            raise ReminderError("cannot snooze for more than a year")
        async with self._db.session() as s:
            row = await self._load(s, reminder_id)
            if row.status not in ("scheduled", "fired"):
                raise ReminderError(f"reminder is {row.status}; nothing to snooze")
            due = aware(row.due_at)
            if row.status == "scheduled" and due > now and target >= due:
                raise ReminderError(
                    f"it is already set to go off sooner, at {format_local(due, cfg.tz, now)}"
                )
            row.status, row.next_at, row.updated_at = "scheduled", target, utcnow()
            await s.commit()
            return self.view(row)

    async def complete(self, reminder_id: int) -> dict[str, Any]:
        """Acknowledge a reminder. A recurring one stays scheduled for its next time."""
        now = self._clock()
        async with self._db.session() as s:
            row = await self._load(s, reminder_id)
            if row.status in ("done", "cancelled"):
                return self.view(row)
            if row.rrule and row.status == "scheduled" and aware(row.due_at) > now:
                row.next_at = row.due_at  # drop a pending snooze, keep the series
            else:
                row.status, row.next_at = "done", None
            row.updated_at = utcnow()
            await s.commit()
            return self.view(row)

    async def cancel(self, reminder_id: int) -> dict[str, Any]:
        async with self._db.session() as s:
            row = await self._load(s, reminder_id)
            row.status, row.next_at, row.updated_at = "cancelled", None, utcnow()
            await s.commit()
            return self.view(row)

    async def delete(self, reminder_id: int) -> None:
        async with self._db.session() as s:
            await s.delete(await self._load(s, reminder_id))
            await s.commit()

    # -- firing -----------------------------------------------------------------------

    async def fire_due(self, now: datetime | None = None) -> int:
        """Fire every reminder whose time has come. Called by the scheduler."""
        now = now or self._clock()
        async with self._db.session() as s:
            ids = (
                await s.scalars(
                    select(Reminder.id)
                    .where(Reminder.status == "scheduled", Reminder.next_at <= now)
                    .order_by(Reminder.next_at)
                )
            ).all()
        fired = 0
        for reminder_id in ids:
            try:
                fired += await self._fire(reminder_id, now)
            except Exception:
                log.exception("failed to fire reminder %s", reminder_id)
        return fired

    async def _fire(self, reminder_id: int, now: datetime) -> int:
        cfg = self._get_config()
        async with self._db.session() as s:
            row = await s.get(Reminder, reminder_id)
            if row is None or row.status != "scheduled" or row.next_at is None:
                return 0
            planned, due = aware(row.next_at), aware(row.due_at)
            if planned > now:
                return 0
            row.last_fired_at, row.fire_count = now, row.fire_count + 1
            if row.rrule and due > now:
                # A snooze went off; the next regular occurrence is still ahead.
                row.next_at = row.due_at
            elif row.rrule:
                tz = _zone(row.timezone, cfg)
                anchor = datetime.fromisoformat(row.anchor)
                # Occurrences missed while the server was down collapse into this one.
                upcoming = next_occurrence(anchor, row.rrule, tz, now)
                if upcoming is None:
                    row.status, row.next_at = "fired", None
                else:
                    row.due_at = row.next_at = upcoming
            else:
                row.status, row.next_at = "fired", None
            row.updated_at = utcnow()
            text, urgency, repeat = row.text, row.urgency, describe_rrule(row.rrule)
            await s.commit()

        body = f"Reminder · {format_local(planned, cfg.tz, now)}"
        if now - planned > LATE_AFTER:
            body += " (delivered late)"
        if repeat:
            body += f" · {repeat}"
        await self._notifier.notify(
            category="reminder", title=text, body=body, urgency=urgency,
            ref=f"reminder:{reminder_id}",
            actions=[
                {"id": "done", "label": "Done"},
                {"id": "snooze", "label": _snooze_label(cfg.notifications.default_snooze_minutes)},
            ],
        )
        return 1

    async def handle_action(self, reminder_id: str, action: str) -> str:
        """Done / Snooze buttons on a notification. Returns a short confirmation."""
        cfg = self._get_config()
        try:
            rid = int(reminder_id)
            if action == "done":
                await self.complete(rid)
                return "Marked done."
            if action == "snooze":
                view = await self.snooze(rid)
                until = datetime.fromisoformat(view["next_at"])
                return f"Snoozed until {format_local(until, cfg.tz, self._clock())}."
        except ReminderError as e:
            return f"Couldn't do that: {e}"
        except ValueError:
            pass
        return "Unknown action."


def _zone(name: str, cfg: AppConfig):
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(name)
    except Exception:
        return cfg.tz


def _snooze_label(minutes: int) -> str:
    if minutes % 60 == 0:
        return f"Snooze {minutes // 60}h"
    return f"Snooze {minutes}m"
