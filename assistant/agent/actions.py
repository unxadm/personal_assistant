"""Pending actions: side-effecting tool calls held until the user approves them."""

from __future__ import annotations

from datetime import UTC
from typing import Any

from sqlalchemy import select

from ..core.db import Database
from ..core.models import Conversation, Message, PendingAction, utcnow
from .tools import ToolContext, ToolRegistry


def as_dict(a: PendingAction) -> dict[str, Any]:
    created = a.created_at if a.created_at.tzinfo else a.created_at.replace(tzinfo=UTC)
    return {
        "id": a.id, "conversation_id": a.conversation_id, "tool": a.tool,
        "arguments": a.arguments, "status": a.status, "result": a.result,
        "created_at": created.isoformat(),
    }


class ActionService:
    """One user's held actions. Approving runs the tool through that user's registry."""

    def __init__(self, db: Database, tools: ToolRegistry, audit: Any = None, user_id: int = 0):
        self._db = db
        self._tools = tools
        self._audit = audit  # AuditService
        self._user_id = user_id

    async def _log(self, actor: str, action: str, summary: str, row: PendingAction) -> None:
        if self._audit is not None:
            await self._audit.record(
                actor, action, summary,
                detail={"action_id": row.id, "tool": row.tool, "arguments": row.arguments,
                        "conversation_id": row.conversation_id},
            )

    async def create(
        self,
        conversation_id: int | None,
        tool: str,
        arguments: dict[str, Any],
        reason: str = "outside content was involved",
    ) -> PendingAction:
        async with self._db.session() as s:
            action = PendingAction(
                user_id=self._user_id, conversation_id=conversation_id, tool=tool,
                arguments=arguments,
            )
            s.add(action)
            await s.commit()
        await self._log(
            "assistant", "action.held",
            f"Held for your approval: {tool.replace('_', ' ')} ({reason})",
            action,
        )
        return action

    async def list(
        self, status: str | None = None, conversation_id: int | None = None
    ) -> list[dict[str, Any]]:
        stmt = (
            select(PendingAction)
            .where(PendingAction.user_id == self._user_id)
            .order_by(PendingAction.id.desc())
            .limit(200)
        )
        if status:
            stmt = stmt.where(PendingAction.status == status)
        if conversation_id is not None:
            stmt = stmt.where(PendingAction.conversation_id == conversation_id)
        async with self._db.session() as s:
            return [as_dict(a) for a in (await s.scalars(stmt)).all()]

    async def resolve(self, action_id: int, approve: bool) -> dict[str, Any] | None:
        """Approve (run the tool) or reject a pending action. None if it is not pending."""
        async with self._db.session() as s:
            action = await s.get(PendingAction, action_id)
            if action is None or action.user_id != self._user_id or action.status != "pending":
                return None
            # Claim it first so a double click cannot run the tool twice.
            action.status = "running" if approve else "rejected"
            action.resolved_at = utcnow()
            await s.commit()

        if approve:
            ctx = ToolContext(conversation_id=action.conversation_id or 0, channel="approval")
            ok, output = await self._tools.call(action.tool, action.arguments, ctx)
            status, note = ("done", "approved and completed") if ok else ("failed", "approved but failed")
        else:
            output, status, note = None, "rejected", "rejected by the user"
            await self._log(
                "user", "action.rejected", f"You rejected: {action.tool.replace('_', ' ')}", action
            )

        async with self._db.session() as s:
            action = await s.get(PendingAction, action_id)
            assert action is not None
            action.status, action.result = status, output
            if action.conversation_id and await s.get(Conversation, action.conversation_id):
                # Keep the thread truthful so the model knows what actually happened.
                text = f"[Action {action.tool} was {note}.]"
                if output:
                    text += f"\nResult: {output[:1000]}"
                s.add(Message(
                    conversation_id=action.conversation_id, role="assistant", content=text,
                    # It was held because outside content was involved; that still holds.
                    meta={"action_id": action.id, "system_note": True, "tainted": True},
                ))
            await s.commit()
            return as_dict(action)
