"""Provider-agnostic LLM adapter for any OpenAI-compatible API.

Features: per-slot endpoints (chat / vision / stt / tts / embeddings), retries with
exponential backoff, a fallback model, and native tool calling with an automatic
JSON-in-prompt fallback for models that reject the ``tools`` parameter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import openai
from openai import AsyncOpenAI

from ..core.config import LLMConfig, LLMSlot
from .json_tools import parse_json_reply, to_json_mode_messages

log = logging.getLogger(__name__)


class LLMError(Exception):
    """The request failed on every configured model."""


class LLMNotConfigured(LLMError):
    """The requested model slot has no model configured."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    usage: dict[str, int] | None = None
    used_fallback: bool = False
    tool_mode: str = "native"  # native | json | none

    def as_message(self) -> dict[str, Any]:
        """The assistant message to append to the history before tool results."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for c in self.tool_calls
            ]
        return msg


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, openai.APIConnectionError):  # includes timeouts
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code in (408, 409, 429) or exc.status_code >= 500
    return False


def _retry_after(exc: Exception) -> float | None:
    if isinstance(exc, openai.APIStatusError):
        try:
            return float(exc.response.headers.get("retry-after", ""))
        except ValueError:
            return None
    return None


def _describe(exc: Exception) -> str:
    if isinstance(exc, openai.APIStatusError):
        return f"HTTP {exc.status_code}: {exc.message}"
    return f"{type(exc).__name__}: {exc}"


class LLMClient:
    def __init__(
        self,
        get_config: Callable[[], LLMConfig],
        *,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._get_config = get_config
        self._http_client = http_client
        self._sleep = sleep
        self._clients: dict[tuple[str | None, str | None, float], AsyncOpenAI] = {}
        # (base_url, model) pairs discovered not to support native tool calling.
        self._json_only: set[tuple[str | None, str | None]] = set()

    # -- plumbing ---------------------------------------------------------------------

    @property
    def config(self) -> LLMConfig:
        return self._get_config()

    def _client(self, slot: LLMSlot) -> AsyncOpenAI:
        key = (slot.base_url, slot.api_key, slot.timeout)
        client = self._clients.get(key)
        if client is None:
            client = AsyncOpenAI(
                base_url=slot.base_url,
                # Local servers (Ollama, LM Studio) ignore the key but the SDK requires one.
                api_key=slot.api_key or "not-needed",
                timeout=slot.timeout,
                max_retries=0,  # retries are handled here so they can span models
                http_client=self._http_client,
            )
            self._clients[key] = client
        return client

    def _slot(self, name: str) -> LLMSlot:
        slot = self.config.resolve(name)
        if slot is None or not slot.model:
            raise LLMNotConfigured(f"no model configured for llm.{name}")
        return slot

    async def _with_retries(self, what: str, call: Callable[[], Awaitable[Any]]) -> Any:
        cfg = self.config
        attempt = 0
        while True:
            try:
                return await call()
            except Exception as e:
                if not _is_retryable(e) or attempt >= cfg.max_retries:
                    raise
                delay = _retry_after(e)
                if delay is None:
                    delay = min(cfg.backoff_max, cfg.backoff_base * 2**attempt)
                    delay *= 0.5 + random.random() / 2  # jitter
                attempt += 1
                log.warning(
                    "%s failed (%s); retry %d/%d in %.1fs",
                    what, _describe(e), attempt, cfg.max_retries, delay,
                )
                await self._sleep(min(delay, cfg.backoff_max))

    # -- chat -------------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        slot: str = "chat",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """Run a chat completion, falling back to ``llm.chat.fallback`` if needed."""
        targets = [(self._slot(slot), False)]
        fallback = self.config.resolve("fallback")
        # The fallback stands in for the chat model only; it may not be able to see images.
        if fallback is not None and slot == "chat":
            targets.append((fallback, True))

        last_error: Exception | None = None
        for target, is_fallback in targets:
            try:
                result = await self._chat_on(target, messages, tools, temperature, max_tokens)
                result.used_fallback = is_fallback
                return result
            except LLMError:
                raise
            except Exception as e:
                last_error = e
                log.warning("model %s failed: %s", target.model, _describe(e))
        raise LLMError(_describe(last_error) if last_error else "no model available") from last_error

    async def _chat_on(
        self,
        slot: LLMSlot,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> ChatResult:
        tool_mode = getattr(self.config.chat, "tool_mode", "auto")
        key = (slot.base_url, slot.model)
        use_json = bool(tools) and (tool_mode == "json" or key in self._json_only)

        if tools and not use_json:
            try:
                return await self._complete(slot, messages, tools, temperature, max_tokens)
            except (openai.BadRequestError, openai.UnprocessableEntityError, openai.NotFoundError) as e:
                if tool_mode != "auto" or isinstance(e, openai.NotFoundError) and "tool" not in str(e).lower():
                    raise
                log.info(
                    "model %s rejected native tool calling (%s); switching to JSON-in-prompt",
                    slot.model, _describe(e),
                )
                use_json = True

        if use_json:
            assert tools is not None
            result = await self._complete(
                slot, to_json_mode_messages(messages, tools), None, temperature, max_tokens
            )
            # Only remember the downgrade once JSON mode has actually worked.
            self._json_only.add(key)
            text, calls = parse_json_reply(result.content)
            known = {t["function"]["name"] for t in tools}
            result.content = text
            result.tool_calls = [
                ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=args)
                for name, args in calls
                if name in known
            ]
            if calls and not result.tool_calls:
                result.content = "I tried to use a tool that does not exist."
            result.tool_mode = "json"
            return result

        result = await self._complete(slot, messages, None, temperature, max_tokens)
        result.tool_mode = "none"
        return result

    async def _complete(
        self,
        slot: LLMSlot,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> ChatResult:
        params: dict[str, Any] = {"model": slot.model, "messages": messages}
        # Only send optional parameters that are set: some models reject them.
        if (t := temperature if temperature is not None else slot.temperature) is not None:
            params["temperature"] = t
        if (m := max_tokens if max_tokens is not None else slot.max_tokens) is not None:
            params["max_tokens"] = m
        if tools:
            params["tools"] = tools

        client = self._client(slot)
        response = await self._with_retries(
            f"chat({slot.model})", lambda: client.chat.completions.create(**params)
        )
        if not response.choices:
            raise LLMError(f"model {slot.model} returned no choices")
        message = response.choices[0].message
        calls = []
        for call in message.tool_calls or []:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_invalid_json": call.function.arguments}
            calls.append(
                ToolCall(
                    id=call.id or f"call_{uuid.uuid4().hex[:12]}",
                    name=call.function.name,
                    arguments=args if isinstance(args, dict) else {"value": args},
                )
            )
        usage = None
        if response.usage is not None:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
            }
        return ChatResult(
            content=message.content or "",
            tool_calls=calls,
            model=response.model or slot.model or "",
            usage=usage,
        )

    # -- embeddings -------------------------------------------------------------------

    @property
    def embeddings_model(self) -> str | None:
        slot = self.config.resolve("embeddings")
        return slot.model if slot else None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        slot = self._slot("embeddings")
        client = self._client(slot)
        try:
            response = await self._with_retries(
                f"embeddings({slot.model})",
                lambda: client.embeddings.create(model=slot.model, input=texts),
            )
        except Exception as e:
            raise LLMError(_describe(e)) from e
        return [d.embedding for d in sorted(response.data, key=lambda d: d.index)]

    # -- speech ------------------------------------------------------------------------

    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> str:
        """Speech to text via ``/audio/transcriptions``. The filename's extension tells
        the server the container format."""
        slot = self._slot("stt")
        client = self._client(slot)
        params: dict[str, Any] = {"model": slot.model, "file": (filename, audio, content_type)}
        if language := getattr(slot, "language", None):
            params["language"] = language
        try:
            response = await self._with_retries(
                f"transcribe({slot.model})", lambda: client.audio.transcriptions.create(**params)
            )
        except Exception as e:
            raise LLMError(_describe(e)) from e
        return (response if isinstance(response, str) else response.text or "").strip()

    async def speak(self, text: str) -> tuple[bytes, str]:
        """Text to speech via ``/audio/speech``. Returns ``(audio, mime type)``."""
        slot = self._slot("tts")
        client = self._client(slot)
        fmt = getattr(slot, "format", "mp3")
        try:
            response = await self._with_retries(
                f"speak({slot.model})",
                lambda: client.audio.speech.create(
                    model=slot.model, voice=getattr(slot, "voice", "alloy"), input=text,
                    response_format=fmt,
                ),
            )
        except Exception as e:
            raise LLMError(_describe(e)) from e
        mime = {"mp3": "audio/mpeg", "opus": "audio/ogg", "aac": "audio/aac",
                "flac": "audio/flac", "wav": "audio/wav"}[fmt]
        return response.content, mime

    def configured(self, slot: str) -> bool:
        resolved = self.config.resolve(slot)
        return resolved is not None and bool(resolved.model)

    # -- diagnostics ------------------------------------------------------------------

    async def test_connection(self, slot_name: str = "chat") -> dict[str, Any]:
        """Make one cheap request against a slot. Never raises."""
        try:
            slot = self._slot(slot_name)
        except LLMNotConfigured as e:
            return {"ok": False, "slot": slot_name, "error": str(e)}
        started = asyncio.get_running_loop().time()
        try:
            if slot_name == "embeddings":
                vectors = await self.embed(["ping"])
                detail = f"{len(vectors[0])}-dimensional embeddings"
            elif slot_name in ("chat", "fallback", "vision"):
                # No max_tokens: reasoning models can spend a small budget before answering.
                result = await self._complete(
                    slot, [{"role": "user", "content": "Reply with the single word: pong"}],
                    None, None, None,
                )
                detail = result.content.strip()[:200]
            else:
                models = await self._with_retries(
                    f"models({slot_name})", lambda: self._client(slot).models.list()
                )
                ids = [m.id for m in models.data]
                if slot.model not in ids:
                    raise LLMError(f"endpoint reachable but model {slot.model!r} is not listed")
                detail = "endpoint reachable, model listed"
        except Exception as e:
            return {
                "ok": False, "slot": slot_name, "model": slot.model,
                "error": str(e) if isinstance(e, LLMError) else _describe(e),
            }
        latency_ms = round((asyncio.get_running_loop().time() - started) * 1000)
        return {
            "ok": True, "slot": slot_name, "model": slot.model,
            "detail": detail, "latency_ms": latency_ms,
        }

    async def aclose(self) -> None:
        for client in self._clients.values():
            if self._http_client is None:
                await client.close()
        self._clients.clear()
