"""Plugin SDK: everything a plugin author imports.

    from assistant.plugins.sdk import Item, Plugin, tool

A plugin is a directory with a ``plugin.yaml`` manifest and a Python module containing a
:class:`Plugin` subclass. See docs/PLUGINS.md.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from .context import PluginContext

ItemType = Literal[
    "item", "event", "task", "message", "contact", "document", "transaction", "location"
]
Capability = Literal["data_source", "tools", "triggers", "notification_channel"]


class PluginError(Exception):
    """A plugin operation failed in an expected way (shown to the user / model)."""


class PermissionDenied(PluginError):
    """The plugin tried to use something its manifest does not declare."""


class NeedsAuth(PluginError):
    """The plugin's OAuth connection is missing, revoked, or lacks a required scope."""


# -- manifest -------------------------------------------------------------------------


class Permissions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Hostnames the plugin may call; "*.example.com" wildcards allowed.
    network: list[str] = []
    # Item types the plugin may write to the common data model.
    data_write: list[ItemType] = []
    # May read items written by *other* plugins (its own are always readable).
    data_read: bool = False
    # May send notifications to the user (ctx.notify).
    notify: bool = False


class OAuthSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Known provider ("google") or explicit endpoints for anything else.
    provider: str | None = None
    auth_url: str | None = None
    token_url: str | None = None
    # Always requested.
    scopes: list[str] = []
    # Requested only when the named boolean config option is on, so write access is opt-in:
    #   optional_scopes: {enable_write: ["https://.../calendar.events"]}
    optional_scopes: dict[str, list[str]] = {}


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,30}$")
    version: str
    description: str = ""
    # "<module>:<ClassName>", module relative to the plugin directory.
    entrypoint: str = Field(default="plugin:Plugin", pattern=r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
    # JSON Schema (type: object) for the plugin's config; rendered as a form in the UI.
    config_schema: dict[str, Any] = {"type": "object", "properties": {}}
    # Names of secrets the user must provide; stored encrypted.
    secrets: list[str] = []
    permissions: Permissions = Permissions()
    capabilities: list[Capability] = []
    # Seconds between sync() calls for data sources (None = only on demand / webhook).
    sync_interval: int | None = Field(default=None, ge=30)
    # Whether the plugin accepts inbound webhooks at /api/plugins/<name>/webhook.
    webhook: bool = False
    oauth: OAuthSpec | None = None


# -- data model -----------------------------------------------------------------------


class Item(BaseModel):
    """One normalized record. ``external_id`` must be stable across syncs."""

    external_id: str
    type: ItemType
    title: str = ""
    body: str = ""
    url: str | None = None
    occurred_at: datetime | None = None  # when it happened (messages, transactions)
    starts_at: datetime | None = None  # events
    ends_at: datetime | None = None
    due_at: datetime | None = None  # tasks
    # Who it came from, for the "exclude senders" privacy rule.
    sender: str | None = None
    participants: list[str] = []
    labels: list[str] = []
    # Type-specific extras (amount, tracking number, calendar id, ...).
    data: dict[str, Any] = {}


class Notification(BaseModel):
    """A message for the user, handed to every notification channel."""

    title: str
    body: str = ""
    url: str | None = None
    urgency: Literal["low", "normal", "high"] = "normal"
    # reminder | briefing | suggestion | plugin | system
    category: str = "system"
    # What it is about, e.g. "reminder:12". Channels that support buttons send
    # ``actions`` back to the host together with this reference.
    ref: str | None = None
    # [{"id": "done", "label": "Done"}, ...]
    actions: list[dict[str, str]] = []
    id: int | None = None


# -- tools ----------------------------------------------------------------------------


@dataclass
class ToolSpec:
    description: str
    parameters: dict[str, Any]
    side_effecting: bool
    untrusted: bool
    name: str | None


@dataclass
class PluginTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    side_effecting: bool = False
    untrusted: bool = True


def tool(
    description: str,
    parameters: dict[str, Any] | None = None,
    *,
    side_effecting: bool = False,
    untrusted: bool = True,
    name: str | None = None,
):
    """Expose an async plugin method to the LLM.

    ``parameters`` is the JSON Schema of the keyword arguments. The tool is registered as
    ``<plugin name>_<method name>``.

    ``side_effecting``: set for anything that sends, deletes, pays, or contacts people.
    Such calls need the user's approval when external content is in play.

    ``untrusted``: the tool returns content written by third parties (emails, web pages,
    calendar invites). Leave it on unless the output is purely the user's own data.
    """

    def decorate(fn):
        fn.__tool__ = ToolSpec(
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            side_effecting=side_effecting,
            untrusted=untrusted,
            name=name,
        )
        return fn

    return decorate


# -- base class -----------------------------------------------------------------------


class Plugin:
    """Base class for plugins. Override only what your capabilities need.

    ``self.ctx`` is the plugin's only route to the host: config, secrets, HTTP, storage,
    events. Every method runs under a timeout and an exception guard, so a failure is
    reported in the plugin manager instead of crashing the app.
    """

    def __init__(self, ctx: PluginContext):
        self.ctx = ctx

    async def setup(self) -> None:
        """Called when the plugin is enabled (and after its config changes)."""

    async def teardown(self) -> None:
        """Called when the plugin is disabled or the app shuts down."""

    async def sync(self) -> None:
        """Data sources: fetch new/changed data and call ``ctx.upsert_items``."""

    async def handle_webhook(self, headers: dict[str, str], body: bytes) -> None:
        """Called for authenticated POSTs to the plugin's webhook URL."""

    async def send(self, notification: Notification) -> None:
        """Notification channels: deliver a notification to the user."""
        raise NotImplementedError

    def tools(self) -> list[PluginTool]:
        """Tools exposed to the LLM. Defaults to every ``@tool``-decorated method."""
        found = []
        for attr in dir(type(self)):
            spec: ToolSpec | None = getattr(getattr(type(self), attr), "__tool__", None)
            if spec is not None:
                found.append(
                    PluginTool(
                        name=spec.name or attr,
                        description=spec.description,
                        parameters=spec.parameters,
                        handler=getattr(self, attr),
                        side_effecting=spec.side_effecting,
                        untrusted=spec.untrusted,
                    )
                )
        return found
