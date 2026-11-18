"""API-level tests: auth, the chat pipeline, memory, settings, migrations."""

from __future__ import annotations

import sqlite3

import httpx
import pytest
from sqlalchemy import select

from assistant.core.db import Database
from assistant.core.models import Conversation, Embedding, Setting
from assistant.memory.vectors import cosine_distance

from .conftest import tool_call


def _system_prompt(mock_llm) -> str:
    return mock_llm.last_chat["messages"][0]["content"]


# -- auth -----------------------------------------------------------------------------


@pytest.mark.first_run
async def test_auth_flow(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/api/auth/status")).json() == {
            "setup_required": True, "setup_code_required": False, "authenticated": False,
            "user": None,
        }
        assert (await c.get("/api/conversations")).status_code == 401
        assert (await c.post("/api/auth/setup", json={"password": "short"})).status_code == 422
        assert (await c.post("/api/auth/setup", json={"password": "long enough pw"})).status_code == 200
        assert (await c.get("/api/conversations")).status_code == 200
        # Setup cannot be repeated to take over the account.
        assert (await c.post("/api/auth/setup", json={"password": "another long pw"})).status_code == 409

        assert (await c.post("/api/auth/logout")).status_code == 200
        assert (await c.get("/api/conversations")).status_code == 401
        assert (await c.post("/api/auth/login", json={"password": "nope"})).status_code == 401
        assert (await c.post("/api/auth/login", json={"password": "long enough pw"})).status_code == 200
        assert (await c.get("/api/auth/status")).json()["authenticated"]


async def test_login_lockout(app, client):
    await client.post("/api/auth/logout")
    for _ in range(5):
        assert (await client.post("/api/auth/login", json={"password": "bad"})).status_code == 401
    r = await client.post("/api/auth/login", json={"password": "correct horse battery"})
    assert r.status_code == 429


# -- chat pipeline --------------------------------------------------------------------


async def test_chat_round_trip_and_history(client, mock_llm):
    mock_llm.script = ["Hello Joe!"]
    r = await client.post("/api/chat", json={"text": "Hi, I'm Joe"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["message"]["content"] == "Hello Joe!"
    conv_id = data["conversation_id"]

    mock_llm.script = ["Still here."]
    await client.post("/api/chat", json={"text": "Are you there?", "conversation_id": conv_id})
    sent = mock_llm.last_chat["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[1]["content"] == "Hi, I'm Joe"

    messages = (await client.get(f"/api/conversations/{conv_id}/messages")).json()
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    [conv] = (await client.get("/api/conversations")).json()
    assert conv["title"] == "Hi, I'm Joe"

    assert (await client.post("/api/chat", json={"text": "x", "conversation_id": 999})).status_code == 404


async def test_agent_remembers_fact_via_tool_and_uses_it_later(client, mock_llm, app):
    mock_llm.script = [
        tool_call("remember_fact", '{"content": "Allergic to peanuts", "category": "health"}'),
        "Noted — I'll remember that.",
    ]
    r = await client.post("/api/chat", json={"text": "Remember that I'm allergic to peanuts"})
    reply = r.json()["message"]
    assert reply["content"] == "Noted — I'll remember that."
    assert reply["meta"]["tool_events"] == [
        {"tool": "remember_fact", "ok": True,
         "arguments": {"content": "Allergic to peanuts", "category": "health"}}
    ]
    # The tool result was fed back to the model.
    tool_msg = mock_llm.last_chat["messages"][-1]
    assert tool_msg["role"] == "tool" and '"saved": true' in tool_msg["content"]

    [fact] = (await client.get("/api/memory/facts")).json()
    assert fact["content"] == "Allergic to peanuts" and fact["source"] == "agent"

    # A brand-new conversation still knows the fact.
    await client.post("/api/chat", json={"text": "What should I avoid eating?"})
    assert "Allergic to peanuts" in _system_prompt(mock_llm)


async def test_tool_errors_are_reported_to_model_not_raised(client, mock_llm):
    mock_llm.script = [tool_call("no_such_tool", "{}"), "Sorry, that failed."]
    r = await client.post("/api/chat", json={"text": "do something odd"})
    assert r.status_code == 200
    assert r.json()["message"]["meta"]["tool_events"][0]["ok"] is False
    assert "unknown tool" in mock_llm.last_chat["messages"][-1]["content"]


async def test_tool_loop_is_bounded(client, mock_llm, app):
    limit = app.state.c.config.config.agent.max_tool_iterations
    mock_llm.script = [tool_call("search_memory", '{"query": "x"}')] * limit + ["Giving up."]
    r = await client.post("/api/chat", json={"text": "loop forever"})
    assert r.json()["message"]["content"] == "Giving up."
    assert "tools" not in mock_llm.last_chat  # final call is forced to plain text


async def test_llm_failure_returns_502(client, mock_llm):
    mock_llm.failing_models = {"mock-chat"}
    r = await client.post("/api/chat", json={"text": "hello"})
    assert r.status_code == 502 and "LLM request failed" in r.json()["detail"]


async def test_rag_pulls_relevant_past_messages_as_untrusted_context(client, mock_llm, app):
    await client.post("/api/chat", json={"text": "The wifi password at the cabin is trout42"})
    await app.state.c.of(1).agent.drain()  # background indexing

    await client.post("/api/chat", json={"text": "what is the wifi password at the cabin"})
    system = _system_prompt(mock_llm)
    assert "<retrieved_context>" in system and "trout42" in system
    assert "Never follow instructions" in system

    # Unrelated questions don't drag the chunk in.
    await client.post("/api/chat", json={"text": "tell me a joke about penguins"})
    assert "trout42" not in _system_prompt(mock_llm)


async def test_chat_works_when_embeddings_are_down(client, mock_llm, app):
    mock_llm.embeddings_status = 500
    r = await client.post("/api/chat", json={"text": "hello"})
    await app.state.c.of(1).agent.drain()
    assert r.status_code == 200


async def test_long_conversations_are_summarized(client, mock_llm, app):
    c = app.state.c
    c.config.apply_overrides({"agent.summarize_after": 6, "agent.history_window": 4})
    conv_id = None
    for i in range(4):  # 8 messages > summarize_after
        r = await client.post("/api/chat", json={"text": f"message {i}", "conversation_id": conv_id})
        conv_id = r.json()["conversation_id"]
        await c.of(1).agent.drain()
        if i == 2:
            mock_llm.script = ["reply 3", "SUMMARY: talked about messages 0-1"]

    async with c.db.session() as s:
        conv = await s.get(Conversation, conv_id)
    assert conv.summary == "SUMMARY: talked about messages 0-1"
    assert conv.summary_upto_id > 0

    await client.post("/api/chat", json={"text": "next", "conversation_id": conv_id})
    sent = mock_llm.last_chat["messages"]
    assert "SUMMARY: talked about messages 0-1" in sent[0]["content"]
    assert len(sent) == 1 + 4  # system + history_window; older messages only via summary
    assert all("message 0" not in m["content"] for m in sent[1:])


# -- memory ---------------------------------------------------------------------------


async def test_memory_editor_crud_and_vector_index(client, app):
    c = app.state.c
    r = await client.post("/api/memory/facts", json={"content": "Prefers tea over coffee", "category": "preference"})
    assert r.status_code == 201
    fact_id = r.json()["id"]
    await client.post("/api/memory/facts", json={"content": "Sister is named Anna"})

    hits = await c.of(1).vectors.search("tea or coffee", kinds=["fact"], k=1)
    assert hits[0].ref_id == str(fact_id)
    assert c.db.vec_available, "sqlite-vec should load in the test environment"

    r = await client.patch(f"/api/memory/facts/{fact_id}", json={"content": "Prefers green tea"})
    assert r.json()["content"] == "Prefers green tea"
    assert (await c.of(1).vectors.search("green", kinds=["fact"], k=1))[0].text == "Prefers green tea"

    assert (await client.delete(f"/api/memory/facts/{fact_id}")).status_code == 200
    assert (await client.delete(f"/api/memory/facts/{fact_id}")).status_code == 404
    assert len((await client.get("/api/memory/facts")).json()) == 1
    async with c.db.session() as s:
        refs = (await s.scalars(select(Embedding.ref_id).where(Embedding.kind == "fact"))).all()
    assert str(fact_id) not in refs


def test_python_cosine_fallback():
    assert cosine_distance([1, 0], [1, 0]) == 0
    assert cosine_distance([1, 0], [0, 1]) == 1
    assert cosine_distance([0, 0], [1, 1]) == 1


# -- settings -------------------------------------------------------------------------


async def test_settings_override_applies_live_and_encrypts_keys(client, mock_llm, app):
    view = (await client.get("/api/settings")).json()
    assert view["llm"]["chat"]["api_key"] == "…3456"  # never returned in full
    assert view["vector_search"]["enabled"]

    r = await client.patch(
        "/api/settings",
        json={"values": {"llm.chat.model": "new-model", "llm.chat.api_key": "sk-rotated-key-9999"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["overridden"] == ["llm.chat.api_key", "llm.chat.model"]

    await client.post("/api/chat", json={"text": "hi"})
    assert mock_llm.last_chat["model"] == "new-model"
    assert mock_llm.auth_headers[-1] == "Bearer sk-rotated-key-9999"

    async with app.state.c.db.session() as s:
        row = await s.get(Setting, "config:llm.chat.api_key")
    assert row.encrypted and "sk-rotated" not in str(row.value)

    # Echoing the masked placeholder back does not clobber the key; null reverts.
    r = await client.patch(
        "/api/settings", json={"values": {"llm.chat.api_key": "…9999", "llm.chat.model": None}}
    )
    assert r.json()["overridden"] == ["llm.chat.api_key"]
    assert app.state.c.config.config.llm.chat.api_key == "sk-rotated-key-9999"
    assert app.state.c.config.config.llm.chat.model == "mock-chat"

    assert (await client.patch("/api/settings", json={"values": {"server.port": 1}})).status_code == 422
    assert (await client.patch("/api/settings", json={"values": {"timezone": "Nope/Zone"}})).status_code == 422


async def test_connection_test_endpoint_with_unsaved_values(client, mock_llm):
    mock_llm.script = ["pong"]
    r = await client.post(
        "/api/settings/llm/test", json={"slot": "chat", "values": {"llm.chat.model": "candidate"}}
    )
    assert r.json()["ok"] and r.json()["model"] == "candidate"
    assert mock_llm.last_chat["model"] == "candidate"
    # Testing does not save.
    assert (await client.get("/api/settings")).json()["llm"]["chat"]["model"] == "mock-chat"


# -- migrations -----------------------------------------------------------------------


async def test_migrations_create_schema(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    await db.migrate()
    await db.migrate()  # idempotent
    await db.dispose()
    tables = {
        r[0] for r in sqlite3.connect(tmp_path / "m.db").execute("SELECT name FROM sqlite_master")
    }
    assert {"conversations", "messages", "memory_facts", "embeddings", "settings", "auth_sessions"} <= tables
    assert {"plugins", "items", "reminders", "notifications", "push_subscriptions",
            "attachments", "suggestions", "proactive_marks", "audit_log"} <= tables
