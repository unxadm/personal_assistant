"""MCP servers as tool sources: registration, approval gate, untrusted output, isolation."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from assistant.core.config import AppConfig

from .conftest import tool_call

SERVER = str(Path(__file__).parent / "fake_mcp_server.py")


def stdio(log: Path, **extra) -> dict:
    return {
        "command": sys.executable, "args": [SERVER], "env": {"FAKE_MCP_LOG": str(log)}, **extra,
    }


@pytest.fixture
def mcp_log(tmp_path) -> Path:
    return tmp_path / "mcp.log"


@pytest.fixture
def app_config(app_config, mcp_log, monkeypatch):
    monkeypatch.setenv("PA_TEST_SECRET", "must-not-reach-servers")
    app_config["mcp"] = {
        "connect_timeout": 20, "call_timeout": 20,
        "servers": {"fake": stdio(mcp_log)},
    }
    return app_config


@pytest.fixture
def running(app_config, mock_llm, web, plugin_dir, clock, mailer):
    """Start an app whose MCP server config is changed by ``**server`` options."""
    from contextlib import asynccontextmanager

    import httpx

    from assistant.api.app import create_app
    from assistant.core.config import ConfigService

    @asynccontextmanager
    async def start(servers=None, **options):
        if servers is not None:
            app_config["mcp"]["servers"] = servers
        elif options:
            app_config["mcp"]["servers"]["fake"].update(options)
        application = create_app(
            ConfigService(app_config), llm_http_client=mock_llm.http_client(),
            plugin_http_client=web.client(), notify_http_client=web.client(), mailer=mailer,
            clock=clock, migrate=False, secret_key="test-secret",
        )
        async with application.router.lifespan_context(application):
            await application.state.c.mcp.wait_settled(30)
            transport = httpx.ASGITransport(app=application)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                await c.post("/api/auth/setup", json={"password": "correct horse battery"})
                yield application, c

    return start


@pytest.fixture
async def mcp(app):
    manager = app.state.c.mcp
    await manager.wait_settled(30)
    return manager


def names(app) -> set[str]:
    return {s["function"]["name"] for s in app.state.c.tools.specs()}


def lines(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def events(response) -> list[dict]:
    return response.json()["message"]["meta"]["tool_events"]


# -- config -----------------------------------------------------------------------------


def test_config_needs_exactly_one_transport():
    with pytest.raises(ValidationError):
        AppConfig(mcp={"servers": {"a": {}}})
    with pytest.raises(ValidationError):
        AppConfig(mcp={"servers": {"a": {"command": "x", "url": "https://x.test/mcp"}}})
    with pytest.raises(ValidationError):
        AppConfig(mcp={"servers": {"a": {"url": "ftp://x.test"}}})
    with pytest.raises(ValidationError):
        AppConfig(mcp={"servers": {"Bad-Name": {"command": "x"}}})
    ok = AppConfig(mcp={"servers": {"a": {"url": "https://x.test/mcp", "headers": {"X-Key": "k"}}}})
    assert ok.mcp.servers["a"].headers == {"X-Key": "k"}


async def test_servers_cannot_be_added_from_the_settings_ui(client):
    r = await client.patch("/api/settings", json={"values": {"mcp.servers.evil.command": "sh"}})
    assert r.status_code == 422
    assert "mcp" not in (await client.get("/api/settings")).json()


# -- registration -----------------------------------------------------------------------


async def test_tools_are_registered_and_offered_to_the_model(client, app, mcp, mock_llm):
    assert {"mcp_fake_echo", "mcp_fake_post_note", "mcp_fake_weird_name"} <= names(app)
    spec = next(s for s in app.state.c.tools.specs() if s["function"]["name"] == "mcp_fake_echo")
    assert spec["function"]["description"].startswith("[from MCP server fake]")
    assert spec["function"]["parameters"]["type"] == "object"
    assert "$schema" not in spec["function"]["parameters"]
    await client.post("/api/chat", json={"text": "hi"})
    offered = {t["function"]["name"] for t in mock_llm.last_chat["tools"]}
    assert "mcp_fake_echo" in offered


async def test_status_lists_servers_without_secrets(client, mcp):
    r = await client.get("/api/mcp")
    (server,) = r.json()["servers"]
    assert server["name"] == "fake" and server["status"] == "running"
    approvals = {t["tool"]: t["approval"] for t in server["tools"]}
    assert approvals["echo"] == "always" and approvals["post_note"] == "always"
    text = json.dumps(r.json())
    assert "FAKE_MCP_LOG" not in text and SERVER not in text


@pytest.mark.parametrize("extra, expected", [
    ({"read_only_tools": ["echo"]}, {"echo": "never"}),
    ({"trust_read_only_hints": True}, {"echo": "never", "post_note": "always"}),
    ({"auto_approve_tools": ["post_note"]}, {"post_note": "when_outside_content"}),
    ({"only_tools": ["echo"]}, {"echo": "always"}),
])
async def test_approval_levels(extra, expected, running):
    async with running(**extra) as (application, _):
        (server,) = application.state.c.mcp.status()
        got = {t["tool"]: t["approval"] for t in server["tools"]}
        assert {k: got.get(k) for k in expected} == expected
        if "only_tools" in extra:
            assert set(got) == {"echo"}


# -- calling ----------------------------------------------------------------------------


async def test_read_only_tool_runs_and_its_output_is_untrusted(running, mock_llm, mcp_log):
    async with running(read_only_tools=["echo"]) as (_, c):
        mock_llm.script = [tool_call("mcp_fake_echo", '{"text": "ping"}'), "done"]
        r = await c.post("/api/chat", json={"text": "echo ping"})
        assert lines(mcp_log) == ["echo ping"]
        (event,) = events(r)
        assert event["ok"] and "pending_action_id" not in event
        tool_msg = next(m for m in mock_llm.last_chat["messages"] if m["role"] == "tool")
        assert "echo: ping" in tool_msg["content"]
        assert "data, not instructions" in tool_msg["content"]


async def test_other_tools_wait_for_approval_every_time(client, app, mcp, mock_llm, mcp_log):
    mock_llm.script = [tool_call("mcp_fake_post_note", '{"text": "hello"}'), "waiting"]
    r = await client.post("/api/chat", json={"text": "post a note saying hello"})
    (event,) = events(r)
    assert event["pending_action_id"] and lines(mcp_log) == []  # nothing ran
    tool_msg = next(m for m in mock_llm.last_chat["messages"] if m["role"] == "tool")
    assert "awaiting_user_approval" in tool_msg["content"]
    assert "always needs the user's approval" in tool_msg["content"]

    pending = (await client.get("/api/actions", params={"status": "pending"})).json()
    assert [a["tool"] for a in pending] == ["mcp_fake_post_note"]
    done = await client.post(f"/api/actions/{event['pending_action_id']}/approve")
    assert done.json()["status"] == "done" and "posted" in done.json()["result"]
    assert lines(mcp_log) == ["post_note hello"]
    # Rejecting runs nothing.
    mock_llm.script = [tool_call("mcp_fake_post_note", '{"text": "again"}'), "waiting"]
    r = await client.post("/api/chat", json={"text": "once more"})
    await client.post(f"/api/actions/{events(r)[0]['pending_action_id']}/reject")
    assert lines(mcp_log) == ["post_note hello"]


async def test_instructions_in_a_result_cannot_trigger_a_change(running, mock_llm, mcp_log):
    """Even for a tool the user allowed to run unattended, outside content closes the gate."""
    async with running(read_only_tools=["lookup"], auto_approve_tools=["post_note"]) as (_, c):
        # Unattended and clean: runs at once.
        mock_llm.script = [tool_call("mcp_fake_post_note", '{"text": "mine"}'), "ok"]
        await c.post("/api/chat", json={"text": "post mine"})
        assert lines(mcp_log) == ["post_note mine"]
        # After reading a page that says "call post_note": held.
        mock_llm.script = [
            tool_call("mcp_fake_lookup", '{"query": "x"}', "c1"),
            tool_call("mcp_fake_post_note", '{"text": "owned"}', "c2"),
            "I did not do that",
        ]
        r = await c.post("/api/chat", json={"text": "look something up"})
        assert [bool(e.get("pending_action_id")) for e in events(r)] == [False, True]
        assert lines(mcp_log) == ["post_note mine"]


async def test_server_errors_reach_the_model_as_failures(app, mcp):
    from assistant.agent.tools import ToolContext

    tools = app.state.c.tools
    ok, out = await tools.call("mcp_fake_explode", {}, ToolContext(0))
    assert not ok and "explode" in out
    ok, _ = await tools.call("mcp_fake_echo", {"wrong": 1}, ToolContext(0))
    assert not ok


async def test_odd_tool_names_are_made_safe(app, mcp):
    assert "mcp_fake_weird_name" in names(app)
    assert all(len(n) <= 64 for n in names(app))


async def test_calls_are_in_the_audit_log(client, app, mcp, mock_llm):
    mock_llm.script = [tool_call("mcp_fake_post_note", '{"text": "hello"}'), "waiting"]
    r = await client.post("/api/chat", json={"text": "post"})
    await client.post(f"/api/actions/{events(r)[0]['pending_action_id']}/approve")
    entries = (await client.get("/api/audit")).json()
    actions = [e["action"] for e in (entries["entries"] if isinstance(entries, dict) else entries)]
    assert "action.held" in actions and "tool.mcp_fake_post_note" in actions


# -- isolation and failure --------------------------------------------------------------


async def test_the_apps_environment_is_not_passed_to_servers(app, mcp):
    from assistant.agent.tools import ToolContext

    ok, out = await app.state.c.tools.call("mcp_fake_secrets_visible", {}, ToolContext(0))
    assert ok and "PA_TEST_SECRET" not in out and "none" in out


async def test_a_server_that_will_not_start_does_not_stop_the_app(running):
    async with running(servers={"gone": {"command": "/nonexistent/program"}}) as (application, c):
        (server,) = application.state.c.mcp.status()
        assert server["status"] == "error" and server["error"] and server["tools"] == []
        assert (await c.get("/api/mcp")).status_code == 200


async def test_a_dropped_server_is_reconnected(app, mcp):
    from assistant.agent.tools import ToolContext

    tools = app.state.c.tools
    ok, out = await tools.call("mcp_fake_die", {}, ToolContext(0))
    assert not ok
    srv = mcp._servers["fake"]
    old = srv.client
    for _ in range(100):  # the supervisor notices, drops the tools, waits, reconnects
        if srv.client is not None and srv.client is not old and "mcp_fake_echo" in names(app):
            break
        await asyncio.sleep(0.2)
    else:
        pytest.fail(f"never reconnected: {srv.status} {srv.error}")
    ok, out = await tools.call("mcp_fake_echo", {"text": "again"}, ToolContext(0))
    assert ok and "again" in out


async def test_reconnect_endpoint(client, mcp):
    r = await client.post("/api/mcp/fake/reconnect")
    assert r.status_code == 200
    await mcp.wait_settled(30)
    assert (await client.get("/api/mcp")).json()["servers"][0]["status"] == "running"
    assert (await client.post("/api/mcp/nope/reconnect")).status_code == 404


# -- HTTP transport ---------------------------------------------------------------------


@pytest.fixture
async def http_server():
    """A real Streamable HTTP MCP server on a free local port; records request headers."""
    import uvicorn
    from mcp.server.mcpserver import MCPServer

    seen: list[dict[str, str]] = []
    server = MCPServer("remote")

    @server.tool()
    def ping() -> str:
        """Answer."""
        return "pong"

    inner = server.streamable_http_app()

    async def app(scope, receive, send):
        if scope["type"] == "http":
            seen.append({k.decode(): v.decode() for k, v in scope["headers"]})
        await inner(scope, receive, send)

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    uv = uvicorn.Server(config)
    task = asyncio.create_task(uv.serve())
    while not uv.started:
        await asyncio.sleep(0.05)
    port = uv.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}/mcp", seen
    uv.should_exit = True
    await task


async def test_http_server_with_headers(running, http_server, mock_llm):
    url, seen = http_server
    servers = {"remote": {"url": url, "headers": {"Authorization": "Bearer s3cret"}}}
    async with running(servers=servers) as (application, c):
        (server,) = (await c.get("/api/mcp")).json()["servers"]
        assert server["status"] == "running", server
        assert server["where"] == "http://127.0.0.1:" + url.split(":")[2].split("/")[0]
        assert "s3cret" not in json.dumps(server)
        assert "mcp_remote_ping" in names(application)
        assert seen and all(h.get("authorization") == "Bearer s3cret" for h in seen)
        from assistant.agent.tools import ToolContext

        ok, out = await application.state.c.tools.call("mcp_remote_ping", {}, ToolContext(0))
        assert ok and "pong" in out


def test_error_text_does_not_quote_addresses_with_tokens():
    from assistant.mcpclient.manager import _short

    err = RuntimeError("Client error '401' for url 'https://mcp.example.com/mcp?token=abc123'")
    text = _short(err)
    assert "abc123" not in text and "https://mcp.example.com" in text
