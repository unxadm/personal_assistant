"""MCP servers as a source of tools for the model.

Each server named under ``mcp.servers`` in config.yaml gets one long-running task that
connects, lists its tools, registers them as ``mcp_<server>_<tool>`` and keeps the
connection open. A server that fails to start or drops is retried with a growing delay
and, after repeated failures, left off until the user asks to reconnect. None of this
blocks start-up or the rest of the app.

What the model sees from a server is written by whoever runs it, so:
- results are always untrusted data (the turn counts as having outside content in it);
- unless the config says a tool only reads, every call waits for the user's approval;
- the tool list is read once per connection, so a server cannot swap tools in mid-session;
- the server is not offered sampling, roots or elicitation: it cannot ask this app's model
  anything or ask the user questions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED
from pydantic import ValidationError

from ..agent.tools import Tool, ToolContext, ToolRegistry
from ..core.config import AppConfig, MCPServerConfig

log = logging.getLogger(__name__)

MAX_FAILURES = 5
MAX_BACKOFF = 300.0
MAX_DESCRIPTION = 1000
MAX_SCHEMA_CHARS = 20_000
MAX_RESULT_CHARS = 50_000
MAX_PAGES = 10
NAME_LIMIT = 64  # what OpenAI-style APIs accept for a function name


class McpToolError(Exception):
    """The server ran the tool and reported that it failed."""


@dataclass
class _Server:
    name: str
    cfg: MCPServerConfig
    status: str = "connecting"  # disabled | connecting | running | error
    error: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    client: Client | None = None
    task: asyncio.Task[None] | None = None
    failures: int = 0
    dropped: asyncio.Event = field(default_factory=asyncio.Event)
    # Set once the first connection attempt has ended, successfully or not.
    settled: asyncio.Event = field(default_factory=asyncio.Event)


class McpManager:
    def __init__(self, tools: ToolRegistry, get_config: Callable[[], AppConfig]):
        self._tools = tools
        self._get_config = get_config
        self._servers: dict[str, _Server] = {}

    # -- lifecycle --------------------------------------------------------------------

    async def start(self) -> None:
        for name, cfg in self._get_config().mcp.servers.items():
            srv = self._servers[name] = _Server(name, cfg)
            if not cfg.enabled:
                srv.status = "disabled"
                continue
            srv.task = asyncio.create_task(self._supervise(srv), name=f"mcp:{name}")

    async def stop(self) -> None:
        for srv in self._servers.values():
            await self._cancel(srv)

    async def reconnect(self, name: str) -> None:
        srv = self._servers[name]
        if not srv.cfg.enabled:
            raise ValueError(f"MCP server {name!r} is turned off in config.yaml")
        await self._cancel(srv)
        srv.failures, srv.error, srv.status = 0, None, "connecting"
        srv.settled.clear()
        srv.task = asyncio.create_task(self._supervise(srv), name=f"mcp:{name}")

    async def wait_settled(self, timeout: float | None = None) -> None:
        """Wait until every enabled server has finished a first connection attempt."""
        pending = [s.settled.wait() for s in self._servers.values() if s.cfg.enabled]
        await asyncio.wait_for(asyncio.gather(*pending), timeout)

    @staticmethod
    async def _cancel(srv: _Server) -> None:
        task, srv.task = srv.task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # -- connection -------------------------------------------------------------------

    async def _supervise(self, srv: _Server) -> None:
        while True:
            started = time.monotonic()
            try:
                await self._serve(srv)
                # It was up and then went away. A server that dies at once, every time,
                # counts as failing.
                srv.failures = srv.failures + 1 if time.monotonic() - started < 60 else 0
                srv.error = "connection lost"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                srv.failures += 1
                srv.error = _short(e)
                log.warning("MCP server %s: %s", srv.name, srv.error)
            finally:
                self._release(srv)
            srv.status = "error"
            srv.settled.set()
            if srv.failures >= MAX_FAILURES:
                srv.error = f"{srv.error} (gave up after {srv.failures} tries; reconnect to retry)"
                log.error("MCP server %s: giving up after %d failures", srv.name, srv.failures)
                return
            await asyncio.sleep(min(2.0 ** srv.failures, MAX_BACKOFF))
            srv.status = "connecting"

    async def _serve(self, srv: _Server) -> None:
        cfg = self._get_config().mcp
        srv.dropped.clear()
        async with AsyncExitStack() as stack:
            async with asyncio.timeout(cfg.connect_timeout):
                client = await stack.enter_async_context(
                    Client(await self._transport(srv.cfg, stack), read_timeout_seconds=cfg.call_timeout)
                )
                listed = await self._list_tools(client)
            srv.client = client
            self._register(srv, client, listed)
            srv.status, srv.error, srv.failures = "running", None, 0
            srv.settled.set()
            log.info("MCP server %s connected (%d tools)", srv.name, len(srv.tools))
            await srv.dropped.wait()

    @staticmethod
    async def _transport(cfg: MCPServerConfig, stack: AsyncExitStack) -> Any:
        if cfg.command:
            return StdioServerParameters(
                command=cfg.command, args=cfg.args, env=cfg.env or None,
                cwd=str(Path(cfg.cwd).expanduser()) if cfg.cwd else None,
            )
        assert cfg.url
        if cfg.url.startswith("http://") and urlsplit(cfg.url).hostname not in (
            "localhost", "127.0.0.1", "::1",
        ):
            log.warning("MCP server at %s is not using HTTPS", _where(cfg.url))
        http = await stack.enter_async_context(
            httpx2.AsyncClient(headers=cfg.headers, timeout=httpx2.Timeout(30.0, read=300.0))
        )
        return streamable_http_client(cfg.url, http_client=http)

    @staticmethod
    async def _list_tools(client: Client) -> list[Any]:
        found: list[Any] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            page = await client.list_tools(cursor=cursor)
            found += page.tools
            cursor = page.next_cursor
            if not cursor:
                break
        return found

    # -- tools ------------------------------------------------------------------------

    def _register(self, srv: _Server, client: Client, listed: list[Any]) -> None:
        cfg, limit = srv.cfg, self._get_config().mcp.max_tools_per_server
        taken: set[str] = set()
        srv.tools = []
        for item in listed:
            if cfg.only_tools and item.name not in cfg.only_tools:
                continue
            if len(srv.tools) >= limit:
                log.warning(
                    "MCP server %s offers more than %d tools; the rest are ignored "
                    "(choose with only_tools)", srv.name, limit,
                )
                break
            schema = _schema(item.input_schema)
            if schema is None:
                log.warning("MCP server %s: tool %r has an unusable schema; skipped", srv.name, item.name)
                continue
            name = _tool_name(srv.name, item.name, taken)
            taken.add(name)
            read_only = item.name in cfg.read_only_tools or (
                cfg.trust_read_only_hints
                and item.annotations is not None
                and item.annotations.read_only_hint is True
            )
            approval = (
                "never" if read_only
                else "when_outside_content" if item.name in cfg.auto_approve_tools
                else "always"
            )
            tool = Tool(
                name=name,
                description=_describe(srv.name, item.description or item.title or ""),
                parameters=schema,
                handler=self._handler(srv, item.name),
                side_effecting=not read_only,
                untrusted_output=True,
                confirm=approval == "always",
            )
            try:
                self._tools.register(tool)
            except ValueError:
                log.warning("MCP server %s: tool name %s is already taken; skipped", srv.name, name)
                continue
            srv.tools.append({"name": name, "tool": item.name, "approval": approval})

    def _release(self, srv: _Server) -> None:
        for entry in srv.tools:
            self._tools.unregister(entry["name"])
        srv.tools, srv.client = [], None

    def _handler(self, srv: _Server, remote_name: str):
        async def handler(_ctx: ToolContext, /, **arguments: Any) -> str:
            client = srv.client
            if client is None:
                raise McpToolError(f"MCP server {srv.name} is not connected")
            timeout = self._get_config().mcp.call_timeout
            try:
                async with asyncio.timeout(timeout):
                    result = await client.call_tool(remote_name, arguments)
            except TimeoutError:
                raise McpToolError(f"{remote_name} timed out after {timeout:g}s") from None
            except MCPError as e:
                if e.error.code == CONNECTION_CLOSED:
                    srv.dropped.set()  # the process or connection is gone: reconnect
                # Otherwise the server answered, with an error.
                raise McpToolError(_clip(str(e.error.message))) from None
            except ValidationError:
                raise McpToolError("the server sent a reply that is not valid MCP") from None
            except Exception:
                srv.dropped.set()  # the connection itself is broken: reconnect
                raise
            text = _render(result)
            if result.is_error:
                raise McpToolError(text or "the tool reported an error")
            return text

        return handler

    # -- presentation -----------------------------------------------------------------

    def status(self) -> list[dict[str, Any]]:
        """What the settings page shows. Never includes commands, arguments, headers or
        environment: they may carry credentials."""
        return [
            {
                "name": s.name,
                "kind": "command" if s.cfg.command else "url",
                "where": _where(s.cfg.url) if s.cfg.url else Path(s.cfg.command or "").name,
                "status": s.status,
                "error": s.error,
                "tools": [dict(t) for t in s.tools],
            }
            for s in self._servers.values()
        ]


# -- helpers --------------------------------------------------------------------------


def _where(url: str) -> str:
    """The address without credentials, path or query."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")


def _short(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):  # anyio task groups wrap the real error
        exc = exc.exceptions[0]
    if isinstance(exc, TimeoutError):
        return "timed out connecting"
    # Error text can quote the address, and an address can carry a token in its query.
    first = re.sub(r"https?://[^\s'\"]+", lambda m: _where(m.group(0)), (str(exc).strip().splitlines() or [""])[0])
    return f"{type(exc).__name__}: {first}"[:300] if first else type(exc).__name__


def _clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n[… {len(text) - limit} more characters cut]"


def _tool_name(server: str, tool: str, taken: set[str]) -> str:
    """``mcp_<server>_<tool>`` with only characters model APIs accept, unique and short."""
    base = f"mcp_{server}_{re.sub(r'[^A-Za-z0-9_-]', '_', tool)}"
    digest = hashlib.sha1(tool.encode()).hexdigest()[:8]
    if len(base) > NAME_LIMIT:
        base = f"{base[: NAME_LIMIT - 9]}_{digest}"
    if base in taken:
        base = f"{base[: NAME_LIMIT - 9]}_{digest}"
    return base


def _describe(server: str, text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text).strip()
    if len(text) > MAX_DESCRIPTION:
        text = text[:MAX_DESCRIPTION] + "…"
    return f"[from MCP server {server}] {text}".strip()


def _schema(raw: Any) -> dict[str, Any] | None:
    schema = dict(raw) if isinstance(raw, dict) else {}
    schema.pop("$schema", None)
    if schema.get("type") not in (None, "object"):
        return None
    schema["type"] = "object"
    schema.setdefault("properties", {})
    return schema if len(json.dumps(schema, default=str)) <= MAX_SCHEMA_CHARS else None


def _render(result: Any) -> str:
    """Text for the model. Binary content is mentioned, not passed on."""
    parts: list[str] = []
    for block in result.content:
        kind = getattr(block, "type", "")
        if kind == "text":
            parts.append(block.text)
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} ({block.mime_type}) not shown]")
        elif kind == "resource":
            res = block.resource
            text = getattr(res, "text", None)
            parts.append(f"[resource {res.uri}]\n{text}" if text is not None else f"[binary resource {res.uri} not shown]")
        elif kind == "resource_link":
            parts.append(f"[resource link: {block.name} {block.uri}]")
    text = "\n".join(p for p in parts if p)
    if not text and result.structured_content:
        text = json.dumps(result.structured_content, default=str)
    return _clip(text)
