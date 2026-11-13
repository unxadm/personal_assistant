"""LLM adapter against the mock OpenAI-compatible server."""

from __future__ import annotations

import pytest

from assistant.llm.client import LLMError
from assistant.llm.json_tools import parse_json_reply, to_json_mode_messages

from .conftest import tool_call

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Weather for a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
]
USER = [{"role": "user", "content": "hi"}]


async def test_plain_chat_sends_configured_model_and_key(make_llm, mock_llm):
    mock_llm.script = ["hello there"]
    result = await make_llm().chat(USER)
    assert result.content == "hello there"
    assert result.model == "mock-chat"
    assert not result.used_fallback
    assert mock_llm.last_chat["model"] == "mock-chat"
    assert mock_llm.auth_headers[-1] == "Bearer sk-test-key-123456"
    # Unset optional parameters are not sent at all.
    assert "temperature" not in mock_llm.last_chat and "max_tokens" not in mock_llm.last_chat


async def test_slot_parameters_are_sent(make_llm, mock_llm):
    await make_llm(chat={"temperature": 0.2, "max_tokens": 99}).chat(USER)
    assert mock_llm.last_chat["temperature"] == 0.2
    assert mock_llm.last_chat["max_tokens"] == 99


async def test_retries_transient_errors_with_exponential_backoff(make_llm, mock_llm):
    mock_llm.script = [500, 429, "recovered"]
    result = await make_llm().chat(USER)
    assert result.content == "recovered"
    assert len(mock_llm.chat_requests) == 3
    first, second = make_llm.sleeps
    # base 0.01 with 50-100% jitter, doubling each attempt
    assert 0.005 <= first <= 0.01
    assert 0.01 <= second <= 0.02


async def test_non_retryable_error_is_not_retried(make_llm, mock_llm):
    mock_llm.script = [401]
    with pytest.raises(LLMError, match="401"):
        await make_llm().chat(USER)
    assert len(mock_llm.chat_requests) == 1
    assert make_llm.sleeps == []


async def test_fallback_model_used_after_retries_exhausted(make_llm, mock_llm):
    mock_llm.failing_models = {"mock-chat"}
    llm = make_llm(chat={"fallback": {"model": "mock-backup"}})
    result = await llm.chat(USER)
    assert result.used_fallback
    assert result.model == "mock-backup"
    models = [r["model"] for r in mock_llm.chat_requests]
    assert models == ["mock-chat"] * 3 + ["mock-backup"]  # 1 try + 2 retries, then fallback
    # The fallback inherits the chat endpoint's key.
    assert mock_llm.auth_headers[-1] == "Bearer sk-test-key-123456"


async def test_error_when_every_model_fails(make_llm, mock_llm):
    mock_llm.failing_models = {"mock-chat", "mock-backup"}
    with pytest.raises(LLMError, match="500"):
        await make_llm(chat={"fallback": {"model": "mock-backup"}}).chat(USER)


async def test_native_tool_calls_are_parsed(make_llm, mock_llm):
    mock_llm.script = [tool_call("get_weather", '{"city": "Oslo"}', "call_abc")]
    result = await make_llm().chat(USER, tools=TOOLS)
    assert result.tool_mode == "native"
    [call] = result.tool_calls
    assert (call.id, call.name, call.arguments) == ("call_abc", "get_weather", {"city": "Oslo"})
    assert mock_llm.last_chat["tools"] == TOOLS
    # Round-trips into an OpenAI-format assistant message.
    assert result.as_message()["tool_calls"][0]["function"]["name"] == "get_weather"


async def test_falls_back_to_json_in_prompt_when_tools_rejected(make_llm, mock_llm):
    mock_llm.supports_tools = False
    mock_llm.script = ['```json\n{"tool_calls": [{"name": "get_weather", "arguments": {"city": "Oslo"}}]}\n```']
    llm = make_llm()
    result = await llm.chat([{"role": "system", "content": "sys"}, *USER], tools=TOOLS)

    assert result.tool_mode == "json"
    assert [(c.name, c.arguments) for c in result.tool_calls] == [("get_weather", {"city": "Oslo"})]
    rejected, retried = mock_llm.chat_requests
    assert "tools" in rejected and "tools" not in retried
    assert "get_weather" in retried["messages"][0]["content"]  # catalogue is in the prompt

    # The downgrade is remembered: the next call skips the native attempt.
    mock_llm.script = ['{"final": "It is sunny."}']
    result = await llm.chat(USER, tools=TOOLS)
    assert result.content == "It is sunny." and not result.tool_calls
    assert len(mock_llm.chat_requests) == 3


async def test_forced_json_mode_never_sends_tools(make_llm, mock_llm):
    mock_llm.script = ["just text, no JSON"]
    result = await make_llm(chat={"tool_mode": "json"}).chat(USER, tools=TOOLS)
    assert result.content == "just text, no JSON"
    assert "tools" not in mock_llm.last_chat


def test_json_mode_rewrites_tool_history():
    history = [
        {"role": "user", "content": "weather?"},
        tool_call("get_weather", '{"city": "Oslo"}'),
        {"role": "tool", "tool_call_id": "call_1", "name": "get_weather", "content": "sunny"},
    ]
    out = to_json_mode_messages(history, TOOLS)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "user"]
    assert '"get_weather"' in out[2]["content"]
    assert "sunny" in out[3]["content"]
    assert all(set(m) == {"role", "content"} for m in out)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"final": "done"}', ("done", [])),
        ('Sure!\n{"tool_calls": [{"name": "a", "arguments": {"x": 1}}]}', ("", [("a", {"x": 1})])),
        ('{"tool_calls": [{"name": "a"}]}', ("", [("a", {})])),
        ("no json here {oops", ("no json here {oops", [])),
        ('{"unrelated": 1}', ('{"unrelated": 1}', [])),
    ],
)
def test_parse_json_reply(text, expected):
    assert parse_json_reply(text) == expected


async def test_embeddings(make_llm, mock_llm):
    vectors = await make_llm().embed(["alpha beta", "gamma"])
    assert len(vectors) == 2 and len(vectors[0]) == 32
    assert mock_llm.embedding_requests[-1]["model"] == "mock-embed"


async def test_connection_test_reports_success_and_failure(make_llm, mock_llm):
    llm = make_llm()
    mock_llm.script = ["pong"]
    ok = await llm.test_connection("chat")
    assert ok["ok"] and ok["detail"] == "pong" and ok["model"] == "mock-chat"
    assert (await llm.test_connection("embeddings"))["ok"]

    mock_llm.script = [401]
    bad = await llm.test_connection("chat")
    assert not bad["ok"] and "401" in bad["error"]
    unset = await llm.test_connection("tts")
    assert not unset["ok"] and "no model configured" in unset["error"]
