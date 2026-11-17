"""REST API: reminders, notifications, push subscriptions, daily briefing."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..llm.client import LLMError
from ..notifications.service import ChannelError
from ..reminders.service import ReminderError, ReminderNotFound
from ..reminders.tools import parse_quick
from .auth import container, require_auth

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

Urgency = Literal["low", "normal", "high"]


def _fail(e: ReminderError) -> HTTPException:
    return HTTPException(404 if isinstance(e, ReminderNotFound) else 422, str(e))


# -- reminders ------------------------------------------------------------------------


class ReminderBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    # Local time (ISO, no offset) or any ISO 8601 timestamp.
    at: str
    rrule: str | None = None
    urgency: Urgency = "normal"


class ReminderPatch(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    at: str | None = None
    rrule: str | None = None  # "" removes the recurrence
    urgency: Urgency | None = None


class QuickBody(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class SnoozeBody(BaseModel):
    minutes: int | None = Field(default=None, ge=1)
    until: str | None = None


@router.get("/reminders")
async def list_reminders(request: Request, all: bool = False):
    return await container(request).reminders.list(include_closed=all)


@router.post("/reminders", status_code=201)
async def create_reminder(body: ReminderBody, request: Request):
    try:
        return await container(request).reminders.create(
            body.text, body.at, rrule=body.rrule, urgency=body.urgency
        )
    except ReminderError as e:
        raise _fail(e) from None


@router.post("/reminders/quick", status_code=201)
async def quick_reminder(body: QuickBody, request: Request):
    """Create a reminder from natural language ("call mum on Sunday at 6")."""
    c = container(request)
    try:
        parsed = await parse_quick(c.llm, body.text, c.clock(), c.config.config)
        return await c.reminders.create(parsed["text"], parsed["at"], rrule=parsed["rrule"])
    except ReminderError as e:
        raise _fail(e) from None
    except LLMError as e:
        raise HTTPException(502, f"LLM request failed: {e}") from None


@router.patch("/reminders/{reminder_id}")
async def update_reminder(reminder_id: int, body: ReminderPatch, request: Request):
    try:
        return await container(request).reminders.update(
            reminder_id, text=body.text, when=body.at, rrule=body.rrule, urgency=body.urgency
        )
    except ReminderError as e:
        raise _fail(e) from None


@router.post("/reminders/{reminder_id}/snooze")
async def snooze_reminder(reminder_id: int, request: Request, body: SnoozeBody | None = None):
    body = body or SnoozeBody()
    try:
        return await container(request).reminders.snooze(
            reminder_id, minutes=body.minutes, until=body.until
        )
    except ReminderError as e:
        raise _fail(e) from None


@router.post("/reminders/{reminder_id}/complete")
async def complete_reminder(reminder_id: int, request: Request):
    try:
        return await container(request).reminders.complete(reminder_id)
    except ReminderError as e:
        raise _fail(e) from None


@router.delete("/reminders/{reminder_id}")
async def delete_reminder(reminder_id: int, request: Request):
    try:
        await container(request).reminders.delete(reminder_id)
    except ReminderError as e:
        raise _fail(e) from None
    return {"ok": True}


# -- notifications --------------------------------------------------------------------


class TestBody(BaseModel):
    channel: str


class SubscriptionBody(BaseModel):
    endpoint: str
    keys: dict[str, str] = {}
    label: str = ""


class UnsubscribeBody(BaseModel):
    endpoint: str


class ActionBody(BaseModel):
    ref: str
    action: str


@router.get("/notifications")
async def notifications(request: Request, limit: int = 50):
    return await container(request).notifier.recent(max(1, min(limit, 200)))


@router.get("/notifications/channels")
async def channels(request: Request):
    c = container(request)
    now = c.clock()
    quiet = c.notifier.quiet_until(now)
    return {
        "channels": await c.notifier.channel_status(),
        "vapid_public_key": await c.webpush.public_key(),
        "devices": await c.webpush.devices(),
        "quiet_until": quiet.isoformat() if quiet else None,
    }


@router.post("/notifications/test")
async def test_channel(body: TestBody, request: Request):
    try:
        await container(request).notifier.test(body.channel)
    except ChannelError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


@router.post("/notifications/action")
async def notification_action(body: ActionBody, request: Request):
    """Buttons on a push notification (called by the service worker)."""
    return {"message": await container(request).notifier.handle_action(body.ref, body.action)}


@router.post("/push/subscribe")
async def subscribe(body: SubscriptionBody, request: Request):
    try:
        await container(request).webpush.subscribe(body.model_dump(), body.label)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return {"ok": True}


@router.post("/push/unsubscribe")
async def unsubscribe(body: UnsubscribeBody, request: Request):
    await container(request).webpush.unsubscribe(body.endpoint)
    return {"ok": True}


# -- briefing -------------------------------------------------------------------------


@router.get("/briefing")
async def briefing(request: Request) -> dict[str, Any]:
    service = container(request).briefing
    return {"latest": await service.latest(), "history": await service.history()}


@router.post("/briefing/run")
async def run_briefing(request: Request):
    return {"latest": await container(request).briefing.run()}
