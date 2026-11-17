"""Configuration: defaults < YAML file < environment variables < settings-UI overrides.

Environment variables use the ``PA_`` prefix and ``__`` for nesting, e.g.
``PA_LLM__CHAT__API_KEY`` sets ``llm.chat.api_key``.
"""

from __future__ import annotations

import copy
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

ENV_PREFIX = "PA_"
# Environment variables with the prefix that are not config paths.
RESERVED_ENV = {"PA_CONFIG", "PA_SECRET_KEY"}
# Only these parts of the config may be changed from the settings UI.
OVERRIDABLE_PREFIXES = ("llm.", "agent.", "privacy.", "notifications.", "briefing.", "proactive.", "timezone")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)


class LLMSlot(_Model):
    """One OpenAI-compatible endpoint + model."""

    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: float = 60.0


class ChatSlot(LLMSlot):
    base_url: str | None = "https://api.openai.com/v1"
    model: str | None = "gpt-4o-mini"
    # auto: try native tool calling, fall back to JSON-in-prompt if the model rejects it.
    tool_mode: Literal["auto", "native", "json"] = "auto"
    fallback: LLMSlot | None = None


class STTSlot(LLMSlot):
    # ISO 639-1 hint ("en", "no"); unset lets the model detect the language.
    language: str | None = None


class TTSSlot(LLMSlot):
    voice: str = "alloy"
    format: Literal["mp3", "opus", "aac", "flac", "wav"] = "mp3"


class LLMConfig(_Model):
    chat: ChatSlot = ChatSlot()
    vision: LLMSlot = LLMSlot()
    stt: STTSlot = STTSlot()
    tts: TTSSlot = TTSSlot()
    embeddings: LLMSlot = LLMSlot()
    max_retries: int = 3
    backoff_base: float = 0.5
    backoff_max: float = 8.0

    @property
    def vision_is_chat(self) -> bool:
        """Images can go straight into the chat request (no separate vision model)."""
        vision = self.resolve("vision")
        return vision is not None and (vision.base_url, vision.model) == (
            self.chat.base_url, self.chat.model,
        )

    def resolve(self, slot: str) -> LLMSlot | None:
        """Return the effective settings for a slot, or None if it is not configured.

        Every slot inherits ``base_url`` and ``api_key`` from ``chat`` when unset.
        ``vision`` and ``fallback`` also inherit the chat model/sampling settings.
        """
        chat = self.chat
        if slot == "chat":
            return chat
        raw = chat.fallback if slot == "fallback" else getattr(self, slot)
        if raw is None:
            return None
        data = raw.model_dump()
        data["base_url"] = data["base_url"] or chat.base_url
        # Only reuse the chat key when talking to the same server.
        if data["api_key"] is None and data["base_url"] == chat.base_url:
            data["api_key"] = chat.api_key
        if slot == "vision":
            data["model"] = data["model"] or chat.model
        if slot in ("vision", "fallback"):
            for key in ("temperature", "max_tokens"):
                if data[key] is None:
                    data[key] = getattr(chat, key)
        if not data["model"]:
            return None
        return type(raw)(**data)


class ServerConfig(_Model):
    host: str = "127.0.0.1"
    port: int = 8000
    # Set true when served over HTTPS so the session cookie is Secure.
    cookie_secure: bool = False
    # Externally reachable base URL (e.g. https://assistant.example.com). Used for OAuth
    # redirects and webhook addresses; defaults to the URL the browser used.
    public_url: str | None = None


class AuthConfig(_Model):
    session_ttl_hours: int = 24 * 14
    min_password_length: int = 10


class AgentConfig(_Model):
    name: str = "Assistant"
    max_tool_iterations: int = 6
    # Number of most recent messages sent verbatim to the model.
    history_window: int = 20
    # Summarize older messages once this many are unsummarized.
    summarize_after: int = 40
    rag_top_k: int = 5
    # Cosine distance above which a retrieved chunk is considered irrelevant.
    rag_max_distance: float = 0.7


class OAuthClient(_Model):
    """OAuth client credentials for one provider (e.g. ``oauth.google``)."""

    client_id: str | None = None
    client_secret: str | None = None


class PrivacyConfig(_Model):
    """Data matching these rules is never stored, embedded, or shown to the LLM."""

    exclude_labels: list[str] = []
    # Email addresses or domains ("boss@example.com", "example.com").
    exclude_senders: list[str] = []
    exclude_calendars: list[str] = []


class PluginsConfig(_Model):
    # Directories scanned for third-party plugins (built-ins are always available).
    dirs: list[str] = ["./plugins"]
    # Run data-source plugins on their sync_interval. Off = sync only on demand/webhook.
    auto_sync: bool = True
    call_timeout: float = 30.0
    sync_timeout: float = 300.0
    # Consecutive failures before a plugin is shut off until re-enabled.
    max_failures: int = 5


def _clock(value: str) -> str:
    try:
        hour, minute = (int(part) for part in str(value).split(":"))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
    except ValueError:
        raise ValueError(f"expected a time like '07:30', got {value!r}") from None
    return f"{hour:02d}:{minute:02d}"


class WebPushConfig(_Model):
    enabled: bool = True
    # Contact sent to the browser's push service with each push (mailto: or https: URL).
    # Defaults to server.public_url.
    contact: str | None = None


# Settings of the SMTP channel that email used before it went through the Gmail API.
LEGACY_EMAIL_KEYS = ("host", "port", "security", "username", "password", "from_addr")


class EmailConfig(_Model):
    """Notification email, sent through the Gmail plugin's connected account."""

    enabled: bool = False
    # Where to send it; empty means the connected Gmail address itself.
    to: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _drop_smtp_settings(cls, data: Any) -> Any:
        # An old config.yaml or .env still naming them must not stop the server starting.
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if k not in LEGACY_EMAIL_KEYS}
        return data


class TelegramConfig(_Model):
    enabled: bool = False
    bot_token: str | None = None
    # Only this chat can talk to the assistant and receives notifications.
    chat_id: str | None = None
    # Accept chat messages from that chat (off = notifications only).
    chat: bool = True
    api_base: str = "https://api.telegram.org"


class QuietHours(_Model):
    enabled: bool = False
    start: str = "22:00"
    end: str = "07:00"

    _check = field_validator("start", "end")(_clock)


Urgency = Literal["low", "normal", "high"]


class NotificationsConfig(_Model):
    web_push: WebPushConfig = WebPushConfig()
    email: EmailConfig = EmailConfig()
    telegram: TelegramConfig = TelegramConfig()
    # Channels used when a category has no entry in `categories`.
    default_channels: list[str] = ["web_push", "telegram"]
    # Category (reminder, briefing, plugin, system) -> channel names.
    categories: dict[str, list[str]] = {}
    # During quiet hours everything except urgency "high" waits until they end.
    quiet_hours: QuietHours = QuietHours()
    default_snooze_minutes: int = 60


class BriefingConfig(_Model):
    enabled: bool = False
    time: str = "07:30"
    # "detailed": a few hundred words under headings, with tomorrow and suggested priorities.
    # "short": at most 150 words.
    detail: Literal["short", "detailed"] = "detailed"

    _check = field_validator("time")(_clock)


Level = Literal["off", "notify", "suggest", "auto"]

# Proactive categories and their default action level.
PROACTIVE_LEVELS: dict[str, str] = {
    # Found in email; accepting creates a reminder or a calendar event.
    "bill": "suggest",
    "deadline": "suggest",
    "appointment": "suggest",
    "commitment": "suggest",
    "travel": "suggest",
    "needs_reply": "suggest",
    # Information only.
    "package": "notify",
    "calendar_conflict": "notify",
    "back_to_back": "notify",
    "early_meeting": "notify",
    "meeting_brief": "notify",
}
# "auto" creates a reminder without asking. Only reminders: they stay inside the app and
# can be undone. Calendar events and anything that reaches other people always wait.
AUTO_CATEGORIES = ("bill", "deadline", "appointment", "commitment", "travel")


class ProactiveConfig(_Model):
    enabled: bool = True
    # Per category: off | notify (tell me, no action offered) | suggest (wait for my
    # approval) | auto (create the reminder straight away). Unlisted = the default.
    levels: dict[str, Level] = {}
    # Below this (after feedback is taken into account) a finding is dropped.
    min_confidence: float = 0.5
    # At or above this it is also sent as a notification; otherwise it waits in the inbox.
    notify_confidence: float = 0.7
    auto_confidence: float = 0.8
    max_notifications_per_hour: int = 3
    max_notifications_per_day: int = 10
    # Ceiling on model calls made in the background.
    max_llm_calls_per_hour: int = 30
    # Mail older than this is not analysed (keeps the first sync cheap).
    lookback_days: int = 3
    # Remind this long before a bill or deadline is due.
    lead_hours: int = 24
    # Two meetings in different places closer together than this get a warning.
    travel_gap_minutes: int = 15
    # Meetings starting before this get a heads-up the evening before.
    early_before: str = "08:00"
    evening_at: str = "18:00"
    # Send a meeting brief this long before it starts (0 = never).
    brief_minutes: int = 30
    # Mail asking for something is flagged when unanswered for this many days.
    reply_after_days: int = 2

    _check = field_validator("early_before", "evening_at")(_clock)

    @field_validator("levels")
    @classmethod
    def _valid_levels(cls, v: dict[str, str]) -> dict[str, str]:
        for category, level in v.items():
            if category not in PROACTIVE_LEVELS:
                raise ValueError(f"unknown proactive category {category!r}")
            if level == "auto" and category not in AUTO_CATEGORIES:
                raise ValueError(f"{category!r} cannot be automatic: there is nothing to do")
        return v

    def level(self, category: str) -> str:
        return self.levels.get(category) or PROACTIVE_LEVELS.get(category, "off")


class MCPServerConfig(_Model):
    """One MCP server whose tools the assistant may call.

    Give either ``command`` (a local program spoken to over stdin/stdout) or ``url`` (a
    Streamable HTTP endpoint). Servers can only be added in config.yaml, never from the
    web UI: a ``command`` is a program run on this machine with this app's permissions.
    """

    enabled: bool = True
    command: str | None = None
    args: list[str] = []
    # Added to a minimal environment (PATH, HOME, ...). The app's own environment,
    # which holds its secrets, is not passed on.
    env: dict[str, str] = {}
    cwd: str | None = None
    url: str | None = None
    # Sent with every request, e.g. {"Authorization": "Bearer ..."}.
    headers: dict[str, str] = {}
    # Expose only these tools (empty = all the server offers).
    only_tools: list[str] = []
    # By default every tool counts as one that changes things, and each call waits for
    # your approval in the app. Name the tools that only read:
    read_only_tools: list[str] = []
    # ... or tools that change things but may run straight away when nothing from outside
    # is involved in the conversation (the way the calendar tools do).
    auto_approve_tools: list[str] = []
    # Believe the server's own readOnlyHint. A server can lie, so this is off by default.
    trust_read_only_hints: bool = False

    @model_validator(mode="after")
    def _one_transport(self) -> MCPServerConfig:
        if bool(self.command) == bool(self.url):
            raise ValueError("set either command (local program) or url (HTTP server), not both")
        if self.url and not self.url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return self


class MCPConfig(_Model):
    """MCP servers, by name (lowercase letters, digits, underscore). Tools show up to the
    model as ``mcp_<server>_<tool>``."""

    servers: dict[str, MCPServerConfig] = {}
    connect_timeout: float = 30.0
    call_timeout: float = 60.0
    # More than this from one server is cut off (use only_tools to choose).
    max_tools_per_server: int = 50

    @field_validator("servers")
    @classmethod
    def _names(cls, v: dict[str, MCPServerConfig]) -> dict[str, MCPServerConfig]:
        for name in v:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", name):
                raise ValueError(
                    f"MCP server name {name!r}: use lowercase letters, digits and _, "
                    "starting with a letter, at most 24 characters"
                )
        return v


class UploadsConfig(_Model):
    max_image_mb: float = 10.0
    max_audio_mb: float = 25.0
    max_images_per_message: int = 4


class SchedulerConfig(_Model):
    enabled: bool = True
    tick_seconds: float = 10.0


class DatabaseConfig(_Model):
    url: str | None = None


class AppConfig(_Model):
    timezone: str = "UTC"
    data_dir: str = "./data"
    log_level: str = "INFO"
    database: DatabaseConfig = DatabaseConfig()
    server: ServerConfig = ServerConfig()
    auth: AuthConfig = AuthConfig()
    llm: LLMConfig = LLMConfig()
    agent: AgentConfig = AgentConfig()
    oauth: dict[str, OAuthClient] = {}
    privacy: PrivacyConfig = PrivacyConfig()
    plugins: PluginsConfig = PluginsConfig()
    mcp: MCPConfig = MCPConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    briefing: BriefingConfig = BriefingConfig()
    proactive: ProactiveConfig = ProactiveConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    uploads: UploadsConfig = UploadsConfig()

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as e:
            raise ValueError(f"unknown timezone {v!r}") from e
        return v

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser()

    @property
    def database_url(self) -> str:
        return self.database.url or f"sqlite+aiosqlite:///{self.data_path / 'assistant.db'}"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def is_secret_path(path: str) -> bool:
    return path.endswith(("api_key", "client_secret", "password", "bot_token"))


def is_overridable(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in OVERRIDABLE_PREFIXES)


def set_path(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def _deep_merge(base: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, raw in env.items():
        if not key.startswith(ENV_PREFIX) or key in RESERVED_ENV or raw == "":
            continue
        path = key[len(ENV_PREFIX) :].lower().replace("__", ".")
        # Secrets stay strings; everything else gets YAML typing ("true", "0.2", ...).
        value = raw if is_secret_path(path) else yaml.safe_load(raw)
        set_path(out, path, value)
    return out


class ConfigService:
    """Holds the effective config and lets the settings UI layer overrides on top."""

    def __init__(self, base: Mapping[str, Any] | None = None):
        self._base: dict[str, Any] = copy.deepcopy(dict(base or {}))
        self._overrides: dict[str, Any] = {}
        self.config = AppConfig.model_validate(self._base)

    @classmethod
    def load(
        cls, path: str | os.PathLike[str] | None = None, env: Mapping[str, str] | None = None
    ) -> ConfigService:
        env = os.environ if env is None else env
        path = path or env.get("PA_CONFIG")
        base: dict[str, Any] = {}
        if path is None and Path("config.yaml").exists():
            path = "config.yaml"
        if path is not None:
            loaded = yaml.safe_load(Path(path).read_text()) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"{path}: top level must be a mapping")
            base = loaded
        _deep_merge(base, _env_overrides(env))
        return cls(base)

    @property
    def overrides(self) -> dict[str, Any]:
        return dict(self._overrides)

    def build(self, overrides: Mapping[str, Any]) -> AppConfig:
        """Validate and return the config that ``overrides`` would produce."""
        data = copy.deepcopy(self._base)
        for path, value in overrides.items():
            if not is_overridable(path):
                raise ValueError(f"{path!r} cannot be changed at runtime")
            set_path(data, path, value)
        return AppConfig.model_validate(data)

    def apply_overrides(self, overrides: Mapping[str, Any]) -> AppConfig:
        """Replace the full override set (dotted path -> value)."""
        self.config = self.build(overrides)
        self._overrides = dict(overrides)
        return self.config
