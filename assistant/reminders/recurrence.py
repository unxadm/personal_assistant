"""Time handling for reminders: parsing times, validating and expanding RRULEs.

Recurrences are evaluated in the user's local wall-clock time, so "every day at 09:00"
stays at 09:00 across daylight-saving changes.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

_WEEKDAYS = {"MO": "Mon", "TU": "Tue", "WE": "Wed", "TH": "Thu", "FR": "Fri", "SA": "Sat", "SU": "Sun"}
_UNITS = {"HOURLY": "hour", "DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}
_UNTIL = re.compile(r"UNTIL=(\d{8}(?:T\d{6})?)(Z?)")


def aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; everything stored is UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_when(value: str | datetime, tz: ZoneInfo) -> datetime:
    """Parse an ISO 8601 date/time. Without an offset it is the user's local time.
    A bare date means 09:00 that day. Returns an aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip()
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(
                f"cannot read {value!r} as a time; use ISO 8601 like 2026-03-14T15:00"
            ) from None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            dt = dt.replace(hour=9)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC)


def local_naive(dt: datetime, tz: ZoneInfo) -> datetime:
    return aware(dt).astimezone(tz).replace(tzinfo=None)


def normalize_rrule(rule: str, tz: ZoneInfo) -> str:
    """Validate an RRULE and return it in canonical form (no prefix, local UNTIL)."""
    text = rule.strip().upper().removeprefix("RRULE:")
    if not text.startswith("FREQ=") and "FREQ=" not in text:
        raise ValueError("recurrence must be an RRULE such as FREQ=WEEKLY;BYDAY=MO")
    freq = re.search(r"FREQ=([A-Z]+)", text)
    if freq and freq.group(1) in ("SECONDLY", "MINUTELY"):
        raise ValueError("reminders cannot repeat more often than hourly")

    def until_local(m: re.Match[str]) -> str:
        stamp, zulu = m.groups()
        if not zulu:
            return m.group(0)
        fmt = "%Y%m%dT%H%M%S" if "T" in stamp else "%Y%m%d"
        local = datetime.strptime(stamp, fmt).replace(tzinfo=UTC).astimezone(tz)
        return "UNTIL=" + local.strftime("%Y%m%dT%H%M%S")

    text = _UNTIL.sub(until_local, text)
    try:
        rrulestr(text, dtstart=datetime(2024, 1, 1, 9, 0))
    except (ValueError, TypeError, KeyError) as e:
        raise ValueError(f"invalid recurrence rule {rule!r}: {e}") from None
    return text


def next_occurrence(
    anchor: datetime, rule: str, tz: ZoneInfo, after: datetime, *, inclusive: bool = False
) -> datetime | None:
    """Next occurrence of ``rule`` (anchored at local naive ``anchor``) after ``after``."""
    occurrences = rrulestr(rule, dtstart=anchor)
    nxt = occurrences.after(local_naive(after, tz), inc=inclusive)
    return None if nxt is None else nxt.replace(tzinfo=tz).astimezone(UTC)


def describe_rrule(rule: str | None) -> str:
    """Short human description: "every day", "every 2 weeks on Mon, Thu"."""
    if not rule:
        return ""
    parts = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    unit = _UNITS.get(parts.get("FREQ", ""))
    if unit is None:
        return "repeats"
    interval = int(parts.get("INTERVAL", "1") or 1)
    days = [d for d in parts.get("BYDAY", "").split(",") if d]
    if unit == "week" and interval == 1 and days == ["MO", "TU", "WE", "TH", "FR"]:
        text = "every weekday"
    else:
        text = f"every {unit}" if interval == 1 else f"every {interval} {unit}s"
        named = [_WEEKDAYS[d] for d in days if d in _WEEKDAYS]
        if named:
            text += " on " + ", ".join(named)
        elif days:
            text += f" ({parts['BYDAY']})"
    if "COUNT" in parts:
        text += f", {parts['COUNT']} times"
    if "UNTIL" in parts:
        u = parts["UNTIL"]
        text += f", until {u[:4]}-{u[4:6]}-{u[6:8]}"
    return text


def format_local(dt: datetime, tz: ZoneInfo, now: datetime | None = None) -> str:
    """"today 14:30", "tomorrow 09:00", "Fri 13 Nov 14:30"."""
    local = aware(dt).astimezone(tz)
    if now is not None:
        today = aware(now).astimezone(tz).date()
        if local.date() == today:
            return f"today {local:%H:%M}"
        if local.date() == today + timedelta(days=1):
            return f"tomorrow {local:%H:%M}"
    text = f"{local:%a} {local.day} {local:%b %H:%M}"
    if now is not None and local.year != aware(now).astimezone(tz).year:
        text = f"{local:%a} {local.day} {local:%b %Y %H:%M}"
    return text
