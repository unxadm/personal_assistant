"""Pure calendar checks for the proactive engine. No I/O, no model: easy to test."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

_VIRTUAL = re.compile(
    r"https?://|zoom|meet\.google|teams|webex|skype|hangout|online|video call|phone|^tbd$", re.I
)


@dataclass
class Event:
    id: int
    title: str
    start: datetime
    end: datetime
    location: str = ""
    all_day: bool = False
    busy: bool = True
    participants: list[str] = field(default_factory=list)
    url: str | None = None


def from_item(item: dict[str, Any]) -> Event | None:
    """Build an :class:`Event` from a stored item (as returned by ``ItemStore.query``)."""
    if not item.get("starts_at"):
        return None
    data = item.get("data") or {}
    start = datetime.fromisoformat(item["starts_at"])
    end = datetime.fromisoformat(item["ends_at"]) if item.get("ends_at") else start
    all_day = bool(data.get("all_day"))
    busy = not (
        all_day
        or data.get("transparent")
        or data.get("my_response") == "declined"
        or data.get("status") == "cancelled"
    )
    return Event(
        id=item["id"], title=item.get("title") or "(no title)", start=start, end=end,
        location=str(data.get("location") or "").strip(), all_day=all_day, busy=busy,
        participants=[p.lower() for p in item.get("participants") or []], url=item.get("url"),
    )


def conflicts(events: list[Event]) -> list[tuple[Event, Event]]:
    """Pairs of busy events whose times overlap."""
    busy = sorted((e for e in events if e.busy and e.end > e.start), key=lambda e: (e.start, e.id))
    pairs = []
    for i, first in enumerate(busy):
        for second in busy[i + 1 :]:
            if second.start >= first.end:
                break
            pairs.append((first, second))
    return pairs


def is_place(location: str) -> bool:
    """A physical location (somewhere you have to travel to), not a call link."""
    return bool(location) and not _VIRTUAL.search(location)


def tight_transitions(events: list[Event], gap: timedelta) -> list[tuple[Event, Event]]:
    """Consecutive busy events in different places with less than ``gap`` between them."""
    busy = sorted((e for e in events if e.busy), key=lambda e: (e.start, e.id))
    pairs = []
    for first, second in zip(busy, busy[1:], strict=False):
        between = second.start - first.end
        if (
            timedelta(0) <= between < gap
            and is_place(first.location)
            and is_place(second.location)
            and first.location.casefold() != second.location.casefold()
        ):
            pairs.append((first, second))
    return pairs


def early_tomorrow(
    events: list[Event], now: datetime, tz: ZoneInfo, before: time, evening: time
) -> list[Event]:
    """Tomorrow's meetings that start before ``before`` — reported from ``evening`` on."""
    local = now.astimezone(tz)
    if local.time() < evening:
        return []
    tomorrow = local.date() + timedelta(days=1)
    return [
        e for e in events
        if e.busy
        and e.start.astimezone(tz).date() == tomorrow
        and e.start.astimezone(tz).time() < before
    ]


def starting_soon(events: list[Event], now: datetime, within: timedelta) -> list[Event]:
    return [e for e in events if e.busy and now < e.start <= now + within]
