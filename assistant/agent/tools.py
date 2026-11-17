"""Tool registry: functions the LLM can call. Plugins will register tools here in Phase 2."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """Per-turn information handed to tool handlers."""

    conversation_id: int
    channel: str = "web"
    # External (untrusted) content has entered this turn. While set, side-effecting tools
    # are held for the user's approval instead of running.
    tainted: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for the arguments object
    handler: Callable[..., Awaitable[Any]]  # async (ctx: ToolContext, **arguments) -> JSON-able
    # Changes something outside the assistant's own memory (send, delete, pay, ...).
    # Later phases require confirmation for these.
    side_effecting: bool = False
    # The result contains third-party content (emails, web pages, invites, ...).
    untrusted_output: bool = False
    # Changes the assistant's own data (reminders, memory). Not gated, but shown as a
    # change in the audit log.
    mutating: bool = False
    # Held for approval, like a side-effecting tool, when outside content is in play.
    # For tools that write things later prompts will trust (long-term memory).
    protected: bool = False
    # Held for approval on every call, not only when outside content is in play. For tools
    # whose effects the app cannot judge (third-party MCP servers).
    confirm: bool = False

    def needs_approval(self, tainted: bool) -> bool:
        return self.confirm or (tainted and (self.side_effecting or self.protected))

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        # async (tool, arguments, ctx, ok, output): called after every tool call.
        self.audit: Callable[..., Awaitable[None]] | None = None

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def specs(self) -> list[dict[str, Any]]:
        return [t.spec() for t in self._tools.values()]

    async def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> tuple[bool, str]:
        """Run a tool. Returns ``(ok, result_text)``; errors are reported to the model, not raised."""
        tool = self._tools.get(name)
        if tool is None:
            return False, json.dumps({"error": f"unknown tool {name!r}"})
        ok, output = await self._run(tool, arguments, ctx)
        if self.audit is not None:
            await self.audit(tool, arguments, ctx, ok, output)
        return ok, output

    @staticmethod
    async def _run(tool: Tool, arguments: dict[str, Any], ctx: ToolContext) -> tuple[bool, str]:
        try:
            result = await tool.handler(ctx, **arguments)
        except TypeError as e:
            return False, json.dumps({"error": f"bad arguments for {tool.name}: {e}"})
        except Exception as e:
            log.exception("tool %s failed", tool.name)
            return False, json.dumps({"error": f"{type(e).__name__}: {e}"})
        return True, result if isinstance(result, str) else json.dumps(result, default=str)
