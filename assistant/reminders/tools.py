"""Reminder tools for the LLM, and natural-language quick-add."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ..agent.tools import Tool, ToolContext, ToolRegistry
from ..core.config import AppConfig
from ..llm.client import LLMClient
from .recurrence import format_local
from .service import ReminderError, ReminderService

RRULE_HELP = (
    "RFC 5545 RRULE for repeating reminders, e.g. FREQ=DAILY, FREQ=WEEKLY;BYDAY=MO,TH, "
    "FREQ=MONTHLY;BYMONTHDAY=1, FREQ=YEARLY, FREQ=WEEKLY;INTERVAL=2;COUNT=10. "
    "Omit for a one-off reminder."
)
WHEN_HELP = (
    "Local date and time, ISO 8601 without offset, e.g. 2026-03-14T15:00. Work it out from "
    "the current date and time in the system prompt."
)


def register_reminder_tools(
    registry: ToolRegistry,
    reminders: ReminderService,
    notifier: Any,
    get_config: Callable[[], AppConfig],
    clock: Callable[[], datetime],
) -> None:
    def summary(view: dict[str, Any]) -> dict[str, Any]:
        cfg, now = get_config(), clock()
        out = {
            "id": view["id"],
            "text": view["text"],
            "due": format_local(datetime.fromisoformat(view["due_at"]), cfg.tz, now),
            "due_at": view["due_local"],
            "status": view["status"],
        }
        if view["repeat"]:
            out["repeats"] = view["repeat"]
        if view["snoozed_until"]:
            out["snoozed_until"] = format_local(datetime.fromisoformat(view["snoozed_until"]), cfg.tz, now)
        return out

    def guarded(fn):
        async def handler(ctx: ToolContext, **kwargs: Any) -> Any:
            try:
                return await fn(ctx, **kwargs)
            except ReminderError as e:
                return {"error": str(e)}

        return handler

    async def create(
        ctx: ToolContext,
        text: str,
        at: str | None = None,
        in_minutes: float | None = None,
        rrule: str | None = None,
        urgency: str = "normal",
    ) -> Any:
        if at is None and in_minutes is None:
            return {"error": "give either `at` or `in_minutes`"}
        when: str | datetime = at if at is not None else clock() + timedelta(minutes=float(in_minutes or 0))
        view = await reminders.create(
            text, when, rrule=rrule, urgency=urgency,
            # Remember when third-party content was in play, so the UI can say so.
            source="external" if ctx.tainted else "agent",
        )
        out = summary(view)
        due = datetime.fromisoformat(view["due_at"])
        quiet_end = notifier.quiet_until(due)
        if quiet_end is not None and urgency != "high":
            out["note"] = (
                "This falls in the user's quiet hours, so the notification will arrive at "
                f"{format_local(quiet_end, get_config().tz, clock())}. Tell the user; use "
                "urgency 'high' only if they want it to come through anyway."
            )
        return out

    async def list_(ctx: ToolContext, include_finished: bool = False) -> Any:
        rows = await reminders.list(include_closed=include_finished)
        return {"reminders": [summary(v) for v in rows]}

    async def update(
        ctx: ToolContext,
        reminder_id: int,
        text: str | None = None,
        at: str | None = None,
        rrule: str | None = None,
        urgency: str | None = None,
    ) -> Any:
        return summary(
            await reminders.update(int(reminder_id), text=text, when=at, rrule=rrule, urgency=urgency)
        )

    async def snooze(
        ctx: ToolContext, reminder_id: int, minutes: float | None = None, until: str | None = None
    ) -> Any:
        return summary(
            await reminders.snooze(
                int(reminder_id), minutes=int(minutes) if minutes else None, until=until
            )
        )

    async def complete(ctx: ToolContext, reminder_id: int) -> Any:
        return summary(await reminders.complete(int(reminder_id)))

    async def cancel(ctx: ToolContext, reminder_id: int) -> Any:
        return summary(await reminders.cancel(int(reminder_id)))

    rid = {"reminder_id": {"type": "integer", "description": "Id from list_reminders."}}
    registry.register(Tool(
        name="create_reminder",
        description=(
            "Set a reminder for the user. They are notified at that time. Use `in_minutes` for "
            "relative requests ('in 20 minutes', 'in 2 hours') and `at` for everything else. "
            "Afterwards confirm the exact date and time you set."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to remind them of, phrased as the reminder itself."},
                "at": {"type": "string", "description": WHEN_HELP + " For a repeating reminder: the first occurrence."},
                "in_minutes": {"type": "number", "description": "Minutes from now."},
                "rrule": {"type": "string", "description": RRULE_HELP},
                "urgency": {
                    "type": "string", "enum": ["low", "normal", "high"],
                    "description": "'high' bypasses quiet hours. Default normal.",
                },
            },
            "required": ["text"],
        },
        handler=guarded(create),
        mutating=True,
    ))
    registry.register(Tool(
        name="list_reminders",
        description="List the user's reminders (upcoming, and ones that went off but are not marked done).",
        parameters={"type": "object", "properties": {"include_finished": {"type": "boolean"}}},
        handler=guarded(list_),
    ))
    registry.register(Tool(
        name="update_reminder",
        description="Change a reminder's text, time, recurrence or urgency. Pass rrule \"\" to stop it repeating.",
        parameters={
            "type": "object",
            "properties": {
                **rid,
                "text": {"type": "string"},
                "at": {"type": "string", "description": WHEN_HELP},
                "rrule": {"type": "string", "description": RRULE_HELP},
                "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
            },
            "required": ["reminder_id"],
        },
        handler=guarded(update),
        side_effecting=True,
    ))
    registry.register(Tool(
        name="snooze_reminder",
        description="Postpone a reminder: it notifies again after `minutes`, or at `until`.",
        parameters={
            "type": "object",
            "properties": {
                **rid,
                "minutes": {"type": "number"},
                "until": {"type": "string", "description": WHEN_HELP},
            },
            "required": ["reminder_id"],
        },
        handler=guarded(snooze),
        side_effecting=True,
    ))
    registry.register(Tool(
        name="complete_reminder",
        description="Mark a reminder as done. A repeating reminder stays active for its next occurrence.",
        parameters={"type": "object", "properties": rid, "required": ["reminder_id"]},
        handler=guarded(complete),
        side_effecting=True,
    ))
    registry.register(Tool(
        name="cancel_reminder",
        description="Cancel a reminder entirely, including all future repeats.",
        parameters={"type": "object", "properties": rid, "required": ["reminder_id"]},
        handler=guarded(cancel),
        side_effecting=True,
    ))


QUICK_PROMPT = """\
Turn the user's request into a reminder. Reply with ONE JSON object and nothing else:
{{"text": "<what to be reminded of>", "at": "<YYYY-MM-DDTHH:MM local time>", "rrule": <RFC 5545 RRULE string or null>}}

Rules:
- It is now {now} ({weekday}), timezone {timezone}. Resolve relative dates from that.
- No time of day given: use 09:00. "Tonight" is 20:00, "this afternoon" 15:00.
- Repeating requests ("every Monday", "daily"): put the first occurrence in "at" and the
  rule in "rrule", e.g. FREQ=WEEKLY;BYDAY=MO. Otherwise "rrule" is null.
- "text" is short and does not repeat the time.
- If no time can be worked out at all, reply {{"error": "<what is missing>"}}.
"""


async def parse_quick(llm: LLMClient, text: str, now: datetime, cfg: AppConfig) -> dict[str, Any]:
    """Natural language -> ``{"text", "at", "rrule"}`` using the chat model."""
    local = now.astimezone(cfg.tz)
    prompt = QUICK_PROMPT.format(
        now=local.strftime("%Y-%m-%dT%H:%M"), weekday=local.strftime("%A"), timezone=cfg.timezone
    )
    result = await llm.chat(
        [{"role": "system", "content": prompt}, {"role": "user", "content": text}]
    )
    match = re.search(r"\{.*\}", result.content, re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        raise ReminderError("Couldn't understand that. Try “call the dentist tomorrow at 9”.")
    if data.get("error") or not data.get("at") or not data.get("text"):
        raise ReminderError(str(data.get("error") or "Couldn't work out when to remind you."))
    return {"text": str(data["text"]), "at": str(data["at"]), "rrule": data.get("rrule") or None}
