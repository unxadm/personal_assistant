"""Notifications forwarded by the Android app, kept as items under the source ``phone``.

Why items: they then get the same treatment as mail and calendar data: embedded for search,
fenced as untrusted when the model sees them, covered by the privacy rules, and deleted
with the user. A notification is third-party text (anyone can send you a message that says
"ignore your instructions"), so nothing here ever reaches the model except through those
paths.

Two things are refused on the way in, whatever the phone sends: one-time codes (a stored
verification code is only a liability) and apps the user listed in ``privacy.exclude_apps``.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from ..core.config import AppConfig
from ..plugins.items import ItemStore
from ..plugins.sdk import Item

SOURCE = "phone"
TITLE_CHARS = 300
TEXT_CHARS = 4000
PURGE_EVERY = timedelta(hours=1)

# A code word next to a short number: "123456 is your verification code", "PIN: 4821".
_CODE_WORD = re.compile(
    r"\b(one[- ]?time|verification|verify|security code|passcode|password|otp|2fa|"
    r"two[- ]factor|login code|sign[- ]?in code|auth(?:entication)? code|pin|code)\b",
    re.IGNORECASE,
)
_DIGITS = re.compile(r"(?<![\w.])\d{4,8}(?![\w])|\b\d{3}[- ]\d{3}\b")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(value: object, limit: int) -> str:
    text = _CONTROL.sub("", value if isinstance(value, str) else "").strip()
    return text[:limit]


def looks_like_a_code(title: str, text: str) -> bool:
    joined = f"{title}\n{text}"
    return bool(_CODE_WORD.search(joined) and _DIGITS.search(joined))


def external_id(package: str, key: str, title: str, text: str) -> str:
    """The same notification posted again (apps refresh them) is one item; a new message in
    the same conversation, with new text, is another."""
    raw = "\x00".join((package, key, title, text))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def to_item(raw: dict[str, Any], now: datetime, retention_days: int) -> Item | None:
    """The item for one notification, or None if it should not be kept."""
    package = clean(raw.get("package"), 200)
    title = clean(raw.get("title"), TITLE_CHARS)
    text = clean(raw.get("text"), TEXT_CHARS)
    if not package or not (title or text) or looks_like_a_code(title, text):
        return None
    try:
        posted = datetime.fromtimestamp(int(raw.get("posted_at") or 0) / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    # A phone with a wrong clock must not plant items in the future.
    posted = min(posted, now)
    if posted < now - timedelta(days=retention_days):
        return None
    app = clean(raw.get("app"), 100) or package
    return Item(
        external_id=external_id(package, clean(raw.get("key"), 300), title, text),
        type="item",
        title=f"{app}: {title}"[:TITLE_CHARS] if title else app,
        body=text,
        occurred_at=posted,
        data={
            "source": "phone_notification", "package": package, "app": app,
            "category": clean(raw.get("category"), 40),
        },
    )


def view(row: dict[str, Any]) -> dict[str, Any]:
    data = row["data"] or {}
    return {
        "id": row["id"], "app": data.get("app", ""), "package": data.get("package", ""),
        "title": row["title"], "text": row["body"], "posted_at": row["occurred_at"],
    }


class PhoneService:
    """One user's phone notifications."""

    def __init__(self, items: ItemStore, get_config, clock, user_id: int):
        self._items = items
        self._get_config = get_config
        self._clock = clock
        self._user_id = user_id
        self._last_purge: datetime | None = None

    def cfg(self) -> AppConfig:
        return self._get_config()

    @property
    def enabled(self) -> bool:
        return self.cfg().phone.enabled

    async def ingest(self, batch: list[dict[str, Any]]) -> dict[str, int]:
        cfg = self.cfg()
        now = self._clock()
        items = [i for raw in batch if (i := to_item(raw, now, cfg.phone.retention_days))]
        result = await self._items.upsert(SOURCE, items)
        # Dropped here (codes, too old) or by the privacy rules inside upsert.
        stored = result["created"] + result["updated"]
        return {"received": len(batch), "stored": stored, "skipped": len(batch) - stored - result["unchanged"]}

    async def recent(self, limit: int = 50, *, hours: int | None = None, app: str = "") -> list[dict[str, Any]]:
        after = self._clock() - timedelta(hours=hours) if hours else None
        rows = await self._items.query(plugin=SOURCE, after=after, limit=500)
        rows.sort(key=lambda r: r["occurred_at"] or "", reverse=True)
        needle = app.strip().lower()
        if needle:
            rows = [
                r for r in rows
                if needle in str((r["data"] or {}).get("app", "")).lower()
                or needle == str((r["data"] or {}).get("package", "")).lower()
            ]
        return [view(r) for r in rows[: max(1, min(limit, 200))]]

    async def delete(self, item_id: int) -> bool:
        return await self._items.delete_by_id(SOURCE, item_id)

    async def clear(self) -> int:
        return await self._items.delete_plugin(SOURCE)

    async def purge(self, now: datetime) -> int:
        """Delete what is past the retention period (at most once an hour)."""
        if self._last_purge and now - self._last_purge < PURGE_EVERY:
            return 0
        self._last_purge = now
        return await self._items.purge(SOURCE, now - timedelta(days=self.cfg().phone.retention_days))
