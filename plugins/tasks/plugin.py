"""Example plugin: a local task list. No network, no secrets.

Tasks live in the common data model as ``task`` items, so they are searchable and visible
to the proactive engine like any other data.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from assistant.plugins.sdk import Item, Plugin, PluginError, tool


class TasksPlugin(Plugin):
    async def _tasks(self) -> list[dict[str, Any]]:
        return await self.ctx.query_items(type="task", limit=500)

    async def setup(self) -> None:
        # Housekeeping: drop tasks completed long ago.
        cutoff = datetime.now(UTC) - timedelta(days=self.ctx.config["keep_completed_days"])
        stale = [
            t["external_id"] for t in await self._tasks()
            if t["data"].get("completed_at") and datetime.fromisoformat(t["data"]["completed_at"]) < cutoff
        ]
        await self.ctx.delete_items(stale)

    @tool(
        "Add a task to the user's to-do list.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "notes": {"type": "string"},
                "due": {"type": "string", "description": "ISO 8601 date or date-time (optional)"},
            },
            "required": ["title"],
        },
        untrusted=False,
    )
    async def add(self, title: str, notes: str = "", due: str = "") -> Any:
        if not title.strip():
            raise PluginError("title is empty")
        due_at = None
        if due:
            try:
                due_at = datetime.fromisoformat(due)
            except ValueError:
                raise PluginError(f"due is not an ISO 8601 date: {due!r}") from None
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=self.ctx.timezone)
        task_id = uuid.uuid4().hex[:8]
        await self.ctx.upsert_items(
            [Item(external_id=task_id, type="task", title=title.strip(), body=notes, due_at=due_at,
                  data={"done": False})]
        )
        return {"task_id": task_id, "title": title.strip(), "due": due_at.isoformat() if due_at else None}

    @tool(
        "List tasks on the user's to-do list.",
        {"type": "object", "properties": {"include_done": {"type": "boolean"}}},
        untrusted=False,
    )
    async def list(self, include_done: bool = False) -> Any:
        tasks = [
            {"task_id": t["external_id"], "title": t["title"], "notes": t["body"],
             "due": t["due_at"], "done": bool(t["data"].get("done"))}
            for t in await self._tasks()
            if include_done or not t["data"].get("done")
        ]
        return {"tasks": sorted(tasks, key=lambda t: (t["due"] is None, t["due"] or ""))}

    @tool(
        "Mark a task as done.",
        {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
        untrusted=False,
    )
    async def complete(self, task_id: str) -> Any:
        task = next((t for t in await self._tasks() if t["external_id"] == task_id), None)
        if task is None:
            raise PluginError(f"no task with id {task_id!r}")
        await self.ctx.upsert_items(
            [Item(
                external_id=task_id, type="task", title=task["title"], body=task["body"],
                due_at=datetime.fromisoformat(task["due_at"]) if task["due_at"] else None,
                data={"done": True, "completed_at": datetime.now(UTC).isoformat()},
            )]
        )
        await self.ctx.emit("completed", {"task_id": task_id, "title": task["title"]})
        return {"task_id": task_id, "done": True}
