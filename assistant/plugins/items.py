"""Storage for the common data model, with privacy exclusions and search indexing."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any

from sqlalchemy import delete, or_, select

from ..core.config import PrivacyConfig
from ..core.db import Database
from ..core.events import Event, EventBus
from ..core.models import Item as ItemRow
from ..core.models import utcnow
from ..memory.vectors import VectorStore
from .sdk import Item

KIND = "item"
EMBED_BODY_CHARS = 4000


def _email(value: str) -> str:
    return (parseaddr(value)[1] or value).strip().lower()


def is_excluded(
    privacy: PrivacyConfig,
    *,
    labels: list[str] | tuple[str, ...] = (),
    sender: str | None = None,
    calendar: str | None = None,
) -> bool:
    """True if the user's privacy rules exclude this data from AI processing."""
    blocked_labels = {x.lower() for x in privacy.exclude_labels}
    if blocked_labels and any(label.lower() in blocked_labels for label in labels):
        return True
    if calendar and calendar.lower() in {c.lower() for c in privacy.exclude_calendars}:
        return True
    if sender and privacy.exclude_senders:
        address = _email(sender)
        domain = address.rpartition("@")[2]
        for rule in privacy.exclude_senders:
            rule = rule.strip().lower().lstrip("@")
            if rule and (address == rule or domain == rule or domain.endswith("." + rule)):
                return True
    return False


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def as_dict(row: ItemRow) -> dict[str, Any]:
    def iso(dt: datetime | None) -> str | None:
        dt = _utc(dt)
        return dt.isoformat() if dt else None

    return {
        "id": row.id, "plugin": row.plugin, "external_id": row.external_id, "type": row.type,
        "title": row.title, "body": row.body, "url": row.url,
        "occurred_at": iso(row.occurred_at), "starts_at": iso(row.starts_at),
        "ends_at": iso(row.ends_at), "due_at": iso(row.due_at),
        "participants": row.participants or [], "labels": row.labels or [],
        "data": row.data or {},
    }


def embed_text(plugin: str, item: Item) -> str:
    """What gets embedded (and shown to the model as a retrieved chunk)."""
    when = item.starts_at or item.due_at or item.occurred_at
    lines = [f"[{item.type} from {plugin}] {item.title}".strip()]
    if when:
        lines.append(f"When: {when.isoformat(timespec='minutes')}")
    if item.participants:
        lines.append("People: " + ", ".join(item.participants[:8]))
    if item.body:
        lines.append(item.body[:EMBED_BODY_CHARS])
    return "\n".join(lines)


class ItemStore:
    """One user's synced items."""

    def __init__(
        self, db: Database, vectors: VectorStore, bus: EventBus, get_privacy, user_id: int
    ):
        self._user_id = user_id
        self._db = db
        self._vectors = vectors
        self._bus = bus
        self._get_privacy = get_privacy

    async def upsert(self, plugin: str, items: list[Item]) -> dict[str, int]:
        """Insert or update items. Excluded items are dropped (and removed if present)."""
        privacy: PrivacyConfig = self._get_privacy()
        changed: list[tuple[ItemRow, Item, bool]] = []
        excluded: list[str] = []
        unchanged = 0
        async with self._db.session() as s:
            for item in items:
                if is_excluded(
                    privacy, labels=item.labels, sender=item.sender, calendar=item.data.get("calendar")
                ):
                    excluded.append(item.external_id)
                    continue
                data = dict(item.data)
                if item.sender:
                    data["sender"] = item.sender
                fields = {
                    "type": item.type, "title": item.title, "body": item.body, "url": item.url,
                    "occurred_at": _utc(item.occurred_at), "starts_at": _utc(item.starts_at),
                    "ends_at": _utc(item.ends_at), "due_at": _utc(item.due_at),
                    "participants": item.participants, "labels": item.labels, "data": data,
                }
                digest = hashlib.sha256(
                    json.dumps(fields, sort_keys=True, default=str).encode()
                ).hexdigest()
                row = await s.scalar(
                    select(ItemRow).where(
                        ItemRow.user_id == self._user_id,
                        ItemRow.plugin == plugin, ItemRow.external_id == item.external_id,
                    )
                )
                if row is not None and row.content_hash == digest:
                    unchanged += 1
                    continue
                created = row is None
                if row is None:
                    row = ItemRow(
                        user_id=self._user_id, plugin=plugin, external_id=item.external_id
                    )
                    s.add(row)
                for key, value in fields.items():
                    setattr(row, key, value)
                row.content_hash, row.updated_at = digest, utcnow()
                changed.append((row, item, created))
            await s.commit()

        if excluded:
            await self.delete(plugin, excluded)
        if changed:
            await self._vectors.upsert_many(
                [
                    (KIND, str(row.id), embed_text(plugin, item), {"plugin": plugin, "type": item.type})
                    for row, item, _ in changed
                ]
            )
        for row, item, created in changed:
            await self._bus.emit(
                Event(
                    "item.created" if created else "item.updated",
                    plugin,
                    {"item_id": row.id, "type": item.type, "external_id": item.external_id},
                )
            )
        created_n = sum(1 for *_, c in changed if c)
        return {
            "created": created_n, "updated": len(changed) - created_n,
            "unchanged": unchanged, "excluded": len(excluded),
        }

    async def delete(self, plugin: str, external_ids: list[str]) -> int:
        if not external_ids:
            return 0
        async with self._db.session() as s:
            rows = (
                await s.scalars(
                    select(ItemRow).where(
                        ItemRow.user_id == self._user_id,
                        ItemRow.plugin == plugin, ItemRow.external_id.in_(external_ids),
                    )
                )
            ).all()
            ids = [r.id for r in rows]
            if ids:
                await s.execute(delete(ItemRow).where(ItemRow.id.in_(ids)))
                await s.commit()
        await self._vectors.delete(KIND, [str(i) for i in ids])
        for row in rows:
            await self._bus.emit(
                Event("item.deleted", plugin, {"item_id": row.id, "type": row.type,
                                               "external_id": row.external_id})
            )
        return len(ids)

    async def apply_privacy(self) -> int:
        """Remove stored items that the current privacy rules exclude (after a rule change)."""
        privacy: PrivacyConfig = self._get_privacy()
        doomed: dict[str, list[str]] = {}
        async with self._db.session() as s:
            for row in (
                await s.scalars(select(ItemRow).where(ItemRow.user_id == self._user_id))
            ).all():
                data = row.data or {}
                if is_excluded(
                    privacy, labels=row.labels or [], sender=data.get("sender"),
                    calendar=data.get("calendar"),
                ):
                    doomed.setdefault(row.plugin, []).append(row.external_id)
        return sum([await self.delete(plugin, ids) for plugin, ids in doomed.items()])

    async def delete_plugin(self, plugin: str) -> int:
        async with self._db.session() as s:
            ids = (
                await s.scalars(
                    select(ItemRow.external_id).where(
                        ItemRow.user_id == self._user_id, ItemRow.plugin == plugin
                    )
                )
            ).all()
        return await self.delete(plugin, list(ids))

    async def query(
        self,
        *,
        plugin: str | None = None,
        type: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        text: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Items filtered by plugin/type/time. ``after``/``before`` apply to the item's
        main timestamp (start, due, or occurrence)."""
        stmt = select(ItemRow).where(ItemRow.user_id == self._user_id)
        if plugin:
            stmt = stmt.where(ItemRow.plugin == plugin)
        if type:
            stmt = stmt.where(ItemRow.type == type)
        when = [ItemRow.starts_at, ItemRow.due_at, ItemRow.occurred_at]
        if after:
            stmt = stmt.where(or_(*[c >= _utc(after) for c in when]))
        if before:
            stmt = stmt.where(or_(*[c <= _utc(before) for c in when]))
        if text:
            like = f"%{text}%"
            stmt = stmt.where(or_(ItemRow.title.ilike(like), ItemRow.body.ilike(like)))
        stmt = stmt.order_by(ItemRow.updated_at.desc()).limit(max(1, min(limit, 500)))
        async with self._db.session() as s:
            return [as_dict(r) for r in (await s.scalars(stmt)).all()]
