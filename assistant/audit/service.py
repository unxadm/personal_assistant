"""Audit log: what the assistant did on the user's behalf, and how to undo it."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select

from ..agent.tools import Tool, ToolContext, ToolRegistry
from ..core.db import Database
from ..core.models import AuditEntry, utcnow
from ..reminders.recurrence import aware

log = logging.getLogger(__name__)

MAX_TEXT = 500


def clip(value: Any, limit: int = MAX_TEXT) -> Any:
    """Shorten long strings anywhere inside a JSON-like value."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, dict):
        return {str(k): clip(v, limit) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        return [clip(v, limit) for v in value[:40]]
    return value


def _undo_reminder(arguments: dict[str, Any], result: dict[str, Any]) -> dict[str, Any] | None:
    if "id" not in result:
        return None
    return {"tool": "cancel_reminder", "arguments": {"reminder_id": result["id"]},
            "label": "Cancel the reminder"}


def _undo_fact(arguments: dict[str, Any], result: dict[str, Any]) -> dict[str, Any] | None:
    if "fact_id" not in result:
        return None
    return {"tool": "forget_fact", "arguments": {"fact_id": result["fact_id"]},
            "label": "Forget this fact"}


def _undo_event(arguments: dict[str, Any], result: dict[str, Any]) -> dict[str, Any] | None:
    if not result.get("event_id"):
        return None
    return {
        "tool": "google_calendar_delete_event",
        "arguments": {"event_id": result["event_id"], "calendar_id": arguments.get("calendar_id") or ""},
        "label": "Delete the event",
    }


# Tool name -> how to reverse a successful call. Everything else cannot be undone here
# (a sent message, a deleted event, a cancelled reminder).
UNDO: dict[str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None]] = {
    "create_reminder": _undo_reminder,
    "remember_fact": _undo_fact,
    "google_calendar_create_event": _undo_event,
}

VIA = {
    "approval": " (you approved it)",
    "suggestion": " (from a suggestion you accepted)",
    "telegram": " (asked over Telegram)",
}


def describe_call(name: str, arguments: dict[str, Any]) -> str:
    """One line a person can read: ``create reminder: text=Call Anna, at=2026-03-14T15:00``."""
    parts = []
    for key, value in list(arguments.items())[:4]:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        parts.append(f"{key}={text[:80]}")
    label = name.replace("_", " ")
    return f"{label}: {', '.join(parts)}" if parts else label


class AuditService:
    """One user's audit log. Entries are written with, and read back by, ``user_id``."""

    def __init__(self, db: Database, user_id: int, clock: Callable[[], datetime] = utcnow):
        self._db = db
        self._user_id = user_id
        self._clock = clock

    async def record(
        self,
        actor: str,
        action: str,
        summary: str,
        *,
        change: bool = False,
        detail: dict[str, Any] | None = None,
        undo: dict[str, Any] | None = None,
    ) -> int:
        row = AuditEntry(
            user_id=self._user_id, at=self._clock(), actor=actor, action=action[:80], summary=summary[:1000],
            change=change, detail=clip(detail or {}), undo=undo,
        )
        async with self._db.session() as s:
            s.add(row)
            await s.commit()
        return row.id

    @staticmethod
    def view(row: AuditEntry) -> dict[str, Any]:
        return {
            "id": row.id,
            "at": aware(row.at).isoformat(),
            "actor": row.actor,
            "action": row.action,
            "summary": row.summary,
            "change": row.change,
            "detail": row.detail or {},
            "undo_label": (row.undo or {}).get("label") if row.undone_at is None else None,
            "undone_at": aware(row.undone_at).isoformat() if row.undone_at else None,
        }

    async def list(
        self, *, changes_only: bool = False, limit: int = 100, before_id: int | None = None
    ) -> list[dict[str, Any]]:
        stmt = (
            select(AuditEntry)
            .where(AuditEntry.user_id == self._user_id)
            .order_by(AuditEntry.id.desc())
            .limit(max(1, min(limit, 500)))
        )
        if changes_only:
            stmt = stmt.where(AuditEntry.change.is_(True))
        if before_id:
            stmt = stmt.where(AuditEntry.id < before_id)
        async with self._db.session() as s:
            return [self.view(r) for r in (await s.scalars(stmt)).all()]

    async def undo(self, entry_id: int, tools: ToolRegistry) -> dict[str, Any]:
        """Reverse an entry. Raises LookupError / ValueError when that is not possible."""
        async with self._db.session() as s:
            row = await s.get(AuditEntry, entry_id)
            if row is None or row.user_id != self._user_id:
                raise LookupError("no such entry")
            if row.undone_at is not None:
                raise ValueError("this was already undone")
            if not row.undo:
                raise ValueError("this cannot be undone from here")
            # Claim it first so a double click cannot run the undo twice.
            row.undone_at = self._clock()
            undo, summary = dict(row.undo), row.summary
            await s.commit()

        ok, output = await tools.call(
            undo["tool"], undo.get("arguments") or {}, ToolContext(0, channel="undo")
        )
        failed = not ok or _error_of(output)
        async with self._db.session() as s:
            row = await s.get(AuditEntry, entry_id)
            assert row is not None
            if failed:
                row.undone_at = None
            await s.commit()
            view = self.view(row)
        if failed:
            raise ValueError(f"undo failed: {_error_of(output) or output[:200]}")
        await self.record(
            "user", "undo", f"Undid: {summary}", change=True,
            detail={"entry_id": entry_id, "tool": undo["tool"]},
        )
        return view


def _parse(output: str) -> dict[str, Any]:
    try:
        data = json.loads(output)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _error_of(output: str) -> str:
    error = _parse(output).get("error")
    return str(error) if error else ""


def tool_recorder(audit: AuditService):
    """Hook for :class:`ToolRegistry`: writes one audit entry per tool call."""

    async def record(
        tool: Tool, arguments: dict[str, Any], ctx: ToolContext, ok: bool, output: str
    ) -> None:
        ctx = ctx or ToolContext(0, channel="internal")
        if ctx.channel == "undo":
            return  # the undo itself is recorded by AuditService.undo
        result = _parse(output)
        ok = ok and not result.get("error")
        change = tool.side_effecting or tool.mutating
        builder = UNDO.get(tool.name)
        summary = describe_call(tool.name, arguments) + VIA.get(ctx.channel, "")
        if not ok:
            summary = "Failed: " + summary
        try:
            await audit.record(
                "assistant", f"tool.{tool.name}", summary, change=change and ok,
                detail={
                    "arguments": arguments, "ok": ok, "via": ctx.channel,
                    "conversation_id": ctx.conversation_id or None,
                    "external_content_involved": ctx.tainted,
                    **({"error": str(result["error"])} if result.get("error") else {}),
                },
                undo=builder(arguments, result) if ok and builder else None,
            )
        except Exception:
            log.exception("could not write the audit entry for %s", tool.name)

    return record
