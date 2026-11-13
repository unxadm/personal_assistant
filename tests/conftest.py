"""Shared fixtures, including an in-process mock OpenAI-compatible server."""

from __future__ import annotations

import hashlib
import re
import textwrap
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from assistant.api.app import create_app
from assistant.core.config import ConfigService
from assistant.llm.client import LLMClient

EMBED_DIM = 32


def fake_embedding(text: str) -> list[float]:
    """Deterministic bag-of-words embedding: texts sharing words are close."""
    vec = [0.0] * EMBED_DIM
    for word in text.lower().replace(":", " ").replace(".", " ").replace("?", " ").split():
        vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % EMBED_DIM] += 1.0
    return vec


def tool_call(name: str, arguments: str, call_id: str = "call_1") -> dict[str, Any]:
    """An assistant message containing one native tool call."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
        ],
    }


class MockOpenAI:
    """Scriptable OpenAI-compatible server (chat completions, embeddings, models).

    ``script`` items are consumed one per chat request: an ``int`` returns that HTTP
    status, a ``str`` is the assistant's reply text, a ``dict`` is a full assistant message.
    When the script is empty the reply is ``default_reply``.
    """

    def __init__(self) -> None:
        self.app = FastAPI()
        self.chat_requests: list[dict[str, Any]] = []
        self.embedding_requests: list[dict[str, Any]] = []
        self.auth_headers: list[str | None] = []
        self.script: list[int | str | dict[str, Any]] = []
        self.default_reply = "ok"
        self.supports_tools = True
        self.failing_models: set[str] = set()
        self.embeddings_status = 200
        self.rejects_images = False
        self.transcript = "hello from the recording"
        self.audio_requests: list[tuple[str, bytes]] = []  # (content type, raw multipart body)
        self.speech_requests: list[dict[str, Any]] = []

        @self.app.post("/v1/chat/completions")
        async def chat(request: Request):
            body = await request.json()
            self.chat_requests.append(body)
            self.auth_headers.append(request.headers.get("authorization"))
            if body["model"] in self.failing_models:
                return _error(500, "model overloaded")
            has_image = any(
                isinstance(m.get("content"), list)
                and any(part.get("type") == "image_url" for part in m["content"])
                for m in body["messages"]
            )
            if has_image and self.rejects_images:
                return _error(400, f"{body['model']} does not support image input")
            if "tools" in body and not self.supports_tools:
                return _error(400, f"{body['model']} does not support tools")
            item = self.script.pop(0) if self.script else self.default_reply
            if isinstance(item, int):
                return _error(item, f"scripted failure {item}")
            message = {"role": "assistant", "content": item} if isinstance(item, str) else item
            return {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }

        @self.app.post("/v1/embeddings")
        async def embeddings(request: Request):
            body = await request.json()
            self.embedding_requests.append(body)
            if self.embeddings_status != 200:
                return _error(self.embeddings_status, "embeddings down")
            inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
            return {
                "object": "list",
                "model": body["model"],
                "data": [
                    {"object": "embedding", "index": i, "embedding": fake_embedding(t)}
                    for i, t in enumerate(inputs)
                ],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            }

        @self.app.post("/v1/audio/transcriptions")
        async def transcriptions(request: Request):
            self.audio_requests.append((request.headers.get("content-type", ""), await request.body()))
            self.auth_headers.append(request.headers.get("authorization"))
            return {"text": self.transcript}

        @self.app.post("/v1/audio/speech")
        async def speech(request: Request):
            self.speech_requests.append(await request.json())
            return Response(b"ID3-fake-mp3-audio", media_type="audio/mpeg")

        @self.app.get("/v1/models")
        async def models():
            return {"object": "list", "data": [{"id": "mock-stt", "object": "model"}]}

    def http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app))

    @property
    def last_chat(self) -> dict[str, Any]:
        return self.chat_requests[-1]


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "mock_error"}}, status_code=status)


def base_config(tmp_path, **llm_extra: Any) -> dict[str, Any]:
    llm: dict[str, Any] = {
        "chat": {"base_url": "http://mock/v1", "api_key": "sk-test-key-123456", "model": "mock-chat"},
        "embeddings": {"model": "mock-embed"},
        "max_retries": 2,
        "backoff_base": 0.01,
    }
    for key, value in llm_extra.items():
        if isinstance(value, dict) and isinstance(llm.get(key), dict):
            llm[key].update(value)
        else:
            llm[key] = value
    return {
        "data_dir": str(tmp_path),
        "llm": llm,
        "plugins": {"dirs": [str(tmp_path / "plugins")], "auto_sync": False, "max_failures": 2},
        "oauth": {"google": {"client_id": "client-123", "client_secret": "client-secret-xyz"}},
        "server": {"public_url": "https://assistant.test"},
        "scheduler": {"enabled": False},  # tests call scheduler.tick() themselves
    }


@pytest.fixture
def mock_llm() -> MockOpenAI:
    return MockOpenAI()


@pytest.fixture
def make_llm(mock_llm, tmp_path):
    """Factory for an LLMClient pointed at the mock server; records backoff sleeps."""

    def factory(**llm_extra: Any) -> LLMClient:
        service = ConfigService(base_config(tmp_path, **llm_extra))

        async def fake_sleep(seconds: float) -> None:
            factory.sleeps.append(seconds)

        return LLMClient(lambda: service.config.llm, http_client=mock_llm.http_client(), sleep=fake_sleep)

    factory.sleeps = []
    return factory


class FakeWeb:
    """Fake internet for plugins: route outbound requests to handlers by method + URL regex."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Any]] = []
        self.requests: list[httpx.Request] = []

    def route(self, method: str, pattern: str):
        def register(fn):
            self.routes.insert(0, (method, re.compile(pattern), fn))  # newest wins
            return fn

        return register

    def json(self, method: str, pattern: str, payload: Any, status: int = 200) -> None:
        self.route(method, pattern)(lambda request, match: httpx.Response(status, json=payload))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url.copy_with(query=None))
        for method, pattern, fn in self.routes:
            if method == request.method and (match := pattern.fullmatch(url)):
                result = fn(request, match)
                return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)
        return httpx.Response(404, json={"error": {"message": f"no fake route for {request.method} {url}"}})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def sent(self, method: str, fragment: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and fragment in str(r.url)]


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


@pytest.fixture
def plugin_dir(tmp_path):
    path = tmp_path / "plugins"
    path.mkdir()
    return path


def write_plugin(plugin_dir, name: str, code: str, **manifest: Any):
    """Create a plugin directory with a manifest and ``plugin.py``."""
    import yaml

    folder = plugin_dir / name
    folder.mkdir(exist_ok=True)
    data = {"name": name, "version": "1.0.0", "entrypoint": "plugin:P", "capabilities": ["tools"]}
    data.update(manifest)
    (folder / "plugin.yaml").write_text(yaml.safe_dump(data))
    (folder / "plugin.py").write_text(
        "from assistant.plugins.sdk import *\n" + textwrap.dedent(code)
    )
    return folder


class FakeClock:
    """Controllable "now" for reminders and notifications."""

    def __init__(self) -> None:
        self.now = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)  # a Tuesday

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now += timedelta(**delta)
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def outbox() -> list:
    """Emails "sent" by the app: (EmailConfig, EmailMessage) tuples."""
    return []


@pytest.fixture
def app_config(tmp_path) -> dict[str, Any]:
    """Config dict the ``app`` fixture is built from; tests may edit it first."""
    return base_config(tmp_path)


@pytest.fixture
async def app(app_config, mock_llm, web, plugin_dir, clock, outbox):
    config = ConfigService(app_config)
    application = create_app(
        config, llm_http_client=mock_llm.http_client(), plugin_http_client=web.client(),
        notify_http_client=web.client(), email_sender=lambda cfg, msg: outbox.append((cfg, msg)),
        clock=clock, migrate=False, secret_key="test-secret",
    )
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app):
    """Authenticated HTTP client for the app."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/auth/setup", json={"password": "correct horse battery"})
        assert r.status_code == 200, r.text
        yield c


BOT_TOKEN = "123456:TEST-bot-token-abcdefghijklmnop"
TELEGRAM = {"enabled": True, "bot_token": BOT_TOKEN, "chat_id": "42"}


@pytest.fixture
def telegram(web) -> list[tuple[str, dict[str, Any]]]:
    """Fake Telegram Bot API. Returns the list of (method, payload) calls received."""
    import json

    calls: list[tuple[str, dict[str, Any]]] = []

    @web.route("POST", r"https://api\.telegram\.org/bot[^/]+/(\w+)")
    def handle(request, match):
        calls.append((match.group(1), json.loads(request.content)))
        return {"ok": True, "result": {"message_id": len(calls)}}

    return calls
