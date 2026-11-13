"""Pure calendar logic: conflicts and free-time search. No I/O, easy to test."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo


def parse_time(value: str, tz: ZoneInfo) -> datetime:
    """Parse ISO 8601; naive values are taken to be in the user's timezone."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"not an ISO 8601 date/time: {value!r}") from e
    return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed


def is_busy(event: dict[str, Any]) -> bool:
    """Whether a normalized event actually blocks time."""
    return not (
        event.get("all_day")
        or event.get("transparent")
        or event.get("my_response") == "declined"
        or event.get("status") == "cancelled"
    )


def find_conflicts(events: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Pairs of busy events whose times overlap. Events need ``start``/``end`` datetimes."""
    busy = sorted((e for e in events if is_busy(e)), key=lambda e: e["start"])
    pairs = []
    for i, first in enumerate(busy):
        for second in busy[i + 1 :]:
            if second["start"] >= first["end"]:
                break
            pairs.append((first, second))
    return pairs


def merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def find_free_slots(
    busy: list[tuple[datetime, datetime]],
    window_start: datetime,
    window_end: datetime,
    duration: timedelta,
    tz: ZoneInfo,
    day_start: time = time(9, 0),
    day_end: time = time(17, 0),
    include_weekends: bool = False,
) -> list[tuple[datetime, datetime]]:
    """Gaps of at least ``duration`` inside working hours, in the user's timezone."""
    blocked = merge(busy)
    slots: list[tuple[datetime, datetime]] = []
    day = window_start.astimezone(tz).date()
    last = window_end.astimezone(tz).date()
    while day <= last:
        if include_weekends or day.weekday() < 5:
            cursor = max(window_start, datetime.combine(day, day_start, tz))
            close = min(window_end, datetime.combine(day, day_end, tz))
            for start, end in blocked:
                if end <= cursor or start >= close:
                    continue
                if start - cursor >= duration:
                    slots.append((cursor, start))
                cursor = max(cursor, end)
            if close - cursor >= duration:
                slots.append((cursor, close))
        day += timedelta(days=1)
    return slots
