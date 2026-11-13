"""PluginContext: the permission-checked surface a plugin uses to reach the host."""

from __future__ import annotations

import logging
from datetime import datetime
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete

from ..core.events import Event
from ..core.models import PluginKV
from .items import is_excluded
from .sdk import Item, PermissionDenied

if TYPE_CHECKING:
    from .manager import PluginManager, PluginRecord


class PluginContext:
    def __init__(self, manager: PluginManager, record: PluginRecord):
        self._m = manager
        self._r = record
        self.name = record.manifest.name
        self.log = logging.getLogger(f"plugin.{self.name}")

    # -- config and secrets -----------------------------------------------------------

    @property
    def config(self) -> dict[str, Any]:
        """The plugin's validated config (schema defaults applied)."""
        return dict(self._r.effective_config)

    @property
    def timezone(self) -> ZoneInfo:
        """The user's timezone."""
        return self._m.app_config().tz

    def secret(self, name: str) -> str | None:
        """A secret declared in the manifest's ``secrets`` list."""
        if name not in self._r.manifest.secrets:
            raise PermissionDenied(f"secret {name!r} is not declared in the manifest")
        value = self._r.secrets.get(name)
        return value if isinstance(value, str) else None

    # -- network ----------------------------------------------------------------------

    async def http(
        self, method: str, url: str, *, oauth: bool = False, **kwargs: Any
    ) -> httpx.Response:
        """Make an HTTP request to a host declared in ``permissions.network``.

        ``oauth=True`` adds the plugin's OAuth bearer token, refreshing it when needed.
        Redirects are not followed, so a declared host cannot bounce the request elsewhere.
        """
        host = httpx.URL(url).host
        if not any(fnmatch(host, pattern) for pattern in self._r.manifest.permissions.network):
            raise PermissionDenied(f"network access to {host!r} is not declared in the manifest")
        kwargs.setdefault("timeout", 30.0)
        kwargs["follow_redirects"] = False
        if not oauth:
            return await self._m.http.request(method, url, **kwargs)
        headers = dict(kwargs.pop("headers", None) or {})
        for attempt in range(2):
            token = await self._m.access_token(self._r, force=attempt == 1)
            headers["Authorization"] = f"Bearer {token}"
            response = await self._m.http.request(method, url, headers=headers, **kwargs)
            if response.status_code != 401:
                break
        return response

    # -- data -------------------------------------------------------------------------

    async def upsert_items(self, items: list[Item]) -> dict[str, int]:
        """Write items to the common data model (types must be in ``data_write``).

        Unchanged items are skipped; items matching the user's privacy exclusions are
        dropped. Returns counts: created / updated / unchanged / excluded.
        """
        allowed = set(self._r.manifest.permissions.data_write)
        for item in items:
            if item.type not in allowed:
                raise PermissionDenied(
                    f"writing {item.type!r} items is not declared in permissions.data_write"
                )
        return await self._m.items.upsert(self.name, items)

    async def delete_items(self, external_ids: list[str]) -> int:
        return await self._m.items.delete(self.name, external_ids)

    async def query_items(
        self,
        *,
        type: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        text: str | None = None,
        limit: int = 50,
        all_plugins: bool = False,
    ) -> list[dict[str, Any]]:
        """Read stored items: this plugin's own, or everyone's with ``data_read``."""
        if all_plugins and not self._r.manifest.permissions.data_read:
            raise PermissionDenied("reading other plugins' items requires permissions.data_read")
        return await self._m.items.query(
            plugin=None if all_plugins else self.name,
            type=type, after=after, before=before, text=text, limit=limit,
        )

    def is_excluded(
        self,
        *,
        labels: list[str] | tuple[str, ...] = (),
        sender: str | None = None,
        calendar: str | None = None,
    ) -> bool:
        """Whether the user's privacy rules exclude this data. Tools that return live
        (unsynced) data must filter with this before returning it to the model."""
        return is_excluded(
            self._m.app_config().privacy, labels=labels, sender=sender, calendar=calendar
        )

    # -- events -----------------------------------------------------------------------

    async def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Publish a trigger event for the proactive engine (needs the ``triggers``
        capability). The type is namespaced as ``<plugin>.<event_type>``."""
        if "triggers" not in self._r.manifest.capabilities:
            raise PermissionDenied("emitting events requires the 'triggers' capability")
        prefix = f"{self.name}."
        full = event_type if event_type.startswith(prefix) else prefix + event_type
        await self._m.bus.emit(Event(full, self.name, payload or {}))

    async def notify(
        self, title: str, body: str = "", *, url: str | None = None, urgency: str = "normal"
    ) -> None:
        """Send the user a notification through their configured channels (needs
        ``permissions.notify``). Quiet hours apply unless ``urgency`` is "high"."""
        if not self._r.manifest.permissions.notify:
            raise PermissionDenied("sending notifications requires permissions.notify")
        if urgency not in ("low", "normal", "high"):
            raise ValueError("urgency must be low, normal or high")
        if self._m.notifier is not None:
            await self._m.notifier.notify(
                category="plugin", title=f"{title} ({self.name})", body=body, url=url, urgency=urgency
            )

    # -- small persistent state -------------------------------------------------------

    async def kv_get(self, key: str, default: Any = None) -> Any:
        async with self._m.db.session() as s:
            row = await s.get(PluginKV, (self.name, key))
        return default if row is None else row.value

    async def kv_set(self, key: str, value: Any) -> None:
        """Store a JSON-serializable value (``None`` deletes the key)."""
        async with self._m.db.session() as s:
            if value is None:
                await s.execute(
                    delete(PluginKV).where(PluginKV.plugin == self.name, PluginKV.key == key)
                )
            else:
                row = await s.get(PluginKV, (self.name, key))
                if row is None:
                    s.add(PluginKV(plugin=self.name, key=key, value=value))
                else:
                    row.value = value
            await s.commit()

    # -- webhooks and sync ------------------------------------------------------------

    @property
    def webhook_token(self) -> str:
        return self._r.webhook_token

    @property
    def webhook_base_url(self) -> str | None:
        """Public URL of this plugin's webhook without the token, or None if unknown."""
        base = self._m.public_url()
        if not self._r.manifest.webhook or not base:
            return None
        return f"{base}/api/plugins/{self.name}/webhook"

    @property
    def webhook_url(self) -> str | None:
        """Webhook URL including the secret token as a query parameter."""
        base = self.webhook_base_url
        return f"{base}?token={self._r.webhook_token}" if base else None

    def request_sync(self) -> None:
        """Schedule a sync soon (e.g. from a webhook) without blocking the caller."""
        self._m.schedule_sync(self.name)
