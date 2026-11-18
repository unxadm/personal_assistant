"""Plugin discovery, lifecycle, and fault isolation.

Plugins run in-process, but every entry point (setup, sync, tools, webhooks) goes through
a guard with a timeout and exception capture. Repeated failures shut the plugin off until
the user re-enables it, so a broken plugin cannot take the app down or spam errors.
"""

from __future__ import annotations

import asyncio
import hmac
import importlib
import json
import logging
import secrets as pysecrets
import sys
import types
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import yaml
from pydantic import ValidationError

from ..agent.tools import Tool, ToolContext, ToolRegistry
from ..core.config import AppConfig
from ..core.db import Database
from ..core.events import EventBus
from ..core.models import PluginKV, PluginState, utcnow
from ..core.security import SecretBox
from .context import PluginContext
from .items import ItemStore
from .oauth import OAuthService, is_expired
from .sdk import Manifest, NeedsAuth, Notification, Plugin, PluginError

log = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"
OAUTH_KEY = "_oauth"


@dataclass
class PluginRecord:
    manifest: Manifest
    path: Path
    builtin: bool
    enabled: bool = False
    config: dict[str, Any] = field(default_factory=dict)
    effective_config: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, Any] = field(default_factory=dict)
    webhook_token: str = ""
    # disabled | running | needs_config | needs_auth | error
    status: str = "disabled"
    error: str | None = None
    failures: int = 0
    last_sync_at: datetime | None = None
    instance: Plugin | None = None
    task: asyncio.Task[None] | None = None
    tool_names: list[str] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    token_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _with_defaults(schema: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    out = dict(config)
    for key, prop in (schema.get("properties") or {}).items():
        if key not in out and isinstance(prop, dict) and "default" in prop:
            out[key] = prop["default"]
    return out


class PluginManager:
    """One user's plugins: whether each is on, its settings, secrets and OAuth tokens, its
    synced data and the tools it adds. Plugin code is shared on disk; every user runs
    their own instances of it, so one user's account is never visible to another's."""

    def __init__(
        self,
        db: Database,
        box: SecretBox,
        tools: ToolRegistry,
        bus: EventBus,
        items: ItemStore,
        app_config: Callable[[], AppConfig],
        http: httpx.AsyncClient | None = None,
        *,
        user_id: int,
        oauth: OAuthService | None = None,
    ):
        self.user_id = user_id
        # Imported plugin modules live under a name of their own, so users do not share
        # (or purge) each other's loaded code.
        self.package = f"pa_plugins_u{user_id}"
        self.db = db
        self.box = box
        self.tools = tools
        self.bus = bus
        self.items = items
        self.app_config = app_config
        self._own_http = http is None
        self.http = http or httpx.AsyncClient()
        self.oauth = oauth or OAuthService(self.http)
        self.records: dict[str, PluginRecord] = {}
        # Directories that look like plugins but failed to load: {path: error}.
        self.invalid: dict[str, str] = {}
        # Base URL last seen from the browser, used when server.public_url is unset.
        self.seen_base_url: str | None = None
        self._background: set[asyncio.Task[Any]] = set()
        # NotificationService, set by the app (lets plugins with `notify` permission notify).
        self.notifier: Any = None

    # -- discovery --------------------------------------------------------------------

    def _dirs(self) -> list[tuple[Path, bool]]:
        dirs = [(BUILTIN_DIR, True)]
        dirs += [(Path(d).expanduser().resolve(), False) for d in self.app_config().plugins.dirs]
        return dirs

    async def discover(self) -> None:
        """Scan plugin directories. Safe to call again at runtime: new plugins appear,
        removed ones are stopped, changed manifests restart running plugins."""
        found: dict[str, tuple[Manifest, Path, bool]] = {}
        self.invalid = {}
        for base, builtin in self._dirs():
            if not base.is_dir():
                continue
            for child in sorted(base.iterdir()):
                manifest_file = child / "plugin.yaml"
                if child.name.startswith(("_", ".")) or not manifest_file.is_file():
                    continue
                try:
                    raw = yaml.safe_load(manifest_file.read_text()) or {}
                    manifest = Manifest.model_validate(raw)
                    jsonschema.Draft202012Validator.check_schema(manifest.config_schema)
                except (ValidationError, yaml.YAMLError, jsonschema.SchemaError, OSError) as e:
                    self.invalid[str(child)] = _short(e)
                    log.warning("invalid plugin at %s: %s", child, _short(e))
                    continue
                if manifest.name in found:
                    self.invalid[str(child)] = f"duplicate plugin name {manifest.name!r}"
                    continue
                found[manifest.name] = (manifest, child, builtin)

        for name in list(self.records):
            if name not in found:
                await self._stop(self.records.pop(name))

        for name, (manifest, path, builtin) in found.items():
            record = self.records.get(name)
            if record is None:
                record = PluginRecord(manifest=manifest, path=path, builtin=builtin)
                await self._load_state(record)
                self.records[name] = record
            elif record.manifest != manifest or record.path != path:
                record.manifest, record.path, record.builtin = manifest, path, builtin
                if record.enabled:
                    await self._start(record)

    async def _load_state(self, record: PluginRecord) -> None:
        name = record.manifest.name
        async with self.db.session() as s:
            row = await s.get(PluginState, (self.user_id, name))
            if row is None:
                row = PluginState(
                    user_id=self.user_id, name=name, webhook_token=pysecrets.token_urlsafe(24)
                )
                s.add(row)
                await s.commit()
        record.enabled = row.enabled
        record.config = dict(row.config or {})
        record.webhook_token = row.webhook_token
        record.last_sync_at = row.last_sync_at
        record.error = row.last_error
        record.secrets = json.loads(self.box.decrypt(row.secrets)) if row.secrets else {}

    async def _save(self, record: PluginRecord, *, secrets: bool = False) -> None:
        async with self.db.session() as s:
            row = await s.get(PluginState, (self.user_id, record.manifest.name))
            if row is None:
                return
            row.enabled, row.config = record.enabled, record.config
            row.last_sync_at, row.last_error = record.last_sync_at, record.error
            row.updated_at = utcnow()
            if secrets:
                row.secrets = self.box.encrypt(json.dumps(record.secrets))
            await s.commit()

    # -- lifecycle --------------------------------------------------------------------

    async def start(self) -> None:
        await self.discover()
        for record in self.records.values():
            if record.enabled:
                await self._start(record)

    async def stop(self) -> None:
        for task in list(self._background):
            task.cancel()
        for record in self.records.values():
            await self._stop(record)
        if self._own_http:
            await self.http.aclose()

    def get(self, name: str) -> PluginRecord:
        try:
            return self.records[name]
        except KeyError:
            raise KeyError(f"unknown plugin {name!r}") from None

    async def enable(self, name: str) -> PluginRecord:
        record = self.get(name)
        record.enabled = True
        await self._save(record)
        await self._start(record)
        return record

    async def disable(self, name: str) -> PluginRecord:
        record = self.get(name)
        record.enabled = False
        await self._stop(record)
        record.status, record.error = "disabled", None
        await self._save(record)
        return record

    async def configure(
        self, name: str, config: dict[str, Any] | None, secrets: dict[str, str | None] | None
    ) -> PluginRecord:
        """Update config and/or secrets, then restart the plugin if it is enabled.

        Secrets: a string sets the value, ``None`` removes it, absent keys are unchanged.
        """
        record = self.get(name)
        if config is not None:
            self._validate(record.manifest, config)
            record.config = config
        for key, value in (secrets or {}).items():
            if key not in record.manifest.secrets:
                raise PluginError(f"{key!r} is not a secret of plugin {name}")
            if value:
                record.secrets[key] = value
            else:
                record.secrets.pop(key, None)
        await self._save(record, secrets=True)
        if record.enabled:
            await self._start(record)
        return record

    @staticmethod
    def _validate(manifest: Manifest, config: dict[str, Any]) -> dict[str, Any]:
        effective = _with_defaults(manifest.config_schema, config)
        try:
            jsonschema.validate(effective, manifest.config_schema)
        except jsonschema.ValidationError as e:
            where = ".".join(str(p) for p in e.absolute_path)
            raise PluginError(f"invalid config{' at ' + where if where else ''}: {e.message}") from e
        return effective

    def required_scopes(self, record: PluginRecord) -> list[str]:
        spec = record.manifest.oauth
        if spec is None:
            return []
        config = _with_defaults(record.manifest.config_schema, record.config)
        scopes = list(spec.scopes)
        for option, extra in spec.optional_scopes.items():
            if config.get(option):
                scopes += [s for s in extra if s not in scopes]
        return scopes

    async def _start(self, record: PluginRecord) -> None:
        await self._stop(record)
        name = record.manifest.name
        record.failures, record.error = 0, None

        def blocked(status: str, message: str) -> None:
            record.status, record.error = status, message

        try:
            record.effective_config = self._validate(record.manifest, record.config)
        except PluginError as e:
            return blocked("needs_config", str(e))
        missing = [s for s in record.manifest.secrets if not record.secrets.get(s)]
        if missing:
            return blocked("needs_config", "missing secrets: " + ", ".join(missing))
        if record.manifest.oauth is not None:
            tokens = record.secrets.get(OAUTH_KEY)
            if not tokens:
                return blocked("needs_auth", "account not connected")
            lacking = set(self.required_scopes(record)) - set(tokens.get("scopes", []))
            if lacking:
                return blocked("needs_auth", "additional permission needed — reconnect the account")

        try:
            cls = self._load_class(record)
            record.instance = cls(PluginContext(self, record))
            plugin_tools = record.instance.tools() if "tools" in record.manifest.capabilities else []
        except Exception as e:
            log.exception("plugin %s failed to load", name)
            record.instance = None
            return blocked("error", f"load failed: {_short(e)}")

        record.status = "running"
        timeout = self.app_config().plugins.call_timeout
        if not await self._guard(record, "setup", record.instance.setup, timeout, trip=True):
            record.instance = None
            return

        for pt in plugin_tools:
            tool_name = f"{name}_{pt.name}"
            self.tools.unregister(tool_name)
            self.tools.register(
                Tool(
                    name=tool_name,
                    description=pt.description,
                    parameters=pt.parameters,
                    handler=self._tool_handler(record, tool_name, pt.handler),
                    side_effecting=pt.side_effecting,
                    untrusted_output=pt.untrusted,
                )
            )
            record.tool_names.append(tool_name)

        if (
            "data_source" in record.manifest.capabilities
            and record.manifest.sync_interval
            and self.app_config().plugins.auto_sync
        ):
            record.task = asyncio.create_task(self._sync_loop(record, record.instance))
        log.info("plugin %s started (%d tools)", name, len(record.tool_names))

    async def _stop(self, record: PluginRecord) -> None:
        task, record.task = record.task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._release(record)
        instance, record.instance = record.instance, None
        if instance is not None:
            try:
                await asyncio.wait_for(instance.teardown(), self.app_config().plugins.call_timeout)
            except Exception as e:
                log.warning("plugin %s teardown failed: %s", record.manifest.name, _short(e))
        self._purge_modules(record.manifest.name)

    def _release(self, record: PluginRecord) -> None:
        for tool_name in record.tool_names:
            self.tools.unregister(tool_name)
        record.tool_names = []

    def _load_class(self, record: PluginRecord) -> type[Plugin]:
        """Import the plugin fresh from disk as ``pa_plugins.<name>.<module>``."""
        name = record.manifest.name
        package = f"{self.package}.{name}"
        self._purge_modules(name)
        if self.package not in sys.modules:
            root = types.ModuleType(self.package)
            root.__path__ = []  # type: ignore[attr-defined]
            sys.modules[self.package] = root
        pkg = types.ModuleType(package)
        pkg.__path__ = [str(record.path)]  # type: ignore[attr-defined]
        pkg.__package__ = package
        sys.modules[package] = pkg
        module_name, class_name = record.manifest.entrypoint.split(":")
        importlib.invalidate_caches()
        module = importlib.import_module(f"{package}.{module_name}")
        cls = getattr(module, class_name, None)
        if not (isinstance(cls, type) and issubclass(cls, Plugin)):
            raise PluginError(f"{record.manifest.entrypoint} is not a Plugin subclass")
        return cls

    def _purge_modules(self, name: str) -> None:
        package = f"{self.package}.{name}"
        for mod in [m for m in sys.modules if m == package or m.startswith(package + ".")]:
            del sys.modules[mod]

    # -- fault isolation --------------------------------------------------------------

    async def _fail(self, record: PluginRecord, what: str, exc: BaseException, trip: bool) -> None:
        name = record.manifest.name
        if isinstance(exc, NeedsAuth):
            record.status, record.error = "needs_auth", str(exc)
            self._release(record)
        else:
            record.failures += 1
            detail = "timed out" if isinstance(exc, TimeoutError) else _short(exc)
            record.error = f"{what}: {detail}"
            log.warning("plugin %s %s", name, record.error)
            if trip or record.failures >= self.app_config().plugins.max_failures:
                record.status = "error"
                self._release(record)
                log.error("plugin %s shut off after %d failure(s)", name, record.failures)
        await self._save(record)

    async def _guard(
        self,
        record: PluginRecord,
        what: str,
        call: Callable[[], Awaitable[Any]],
        timeout: float,
        *,
        trip: bool = False,
    ) -> bool:
        """Run a plugin entry point; never raises (except cancellation)."""
        try:
            await asyncio.wait_for(call(), timeout)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._fail(record, what, e, trip)
            return False
        if record.failures or record.error:
            record.failures, record.error = 0, None
            await self._save(record)
        return True

    def _tool_handler(self, record: PluginRecord, tool_name: str, fn: Callable[..., Awaitable[Any]]):
        async def handler(_ctx: ToolContext, **arguments: Any) -> Any:
            if record.status != "running":
                raise PluginError(f"plugin {record.manifest.name} is not running")
            try:
                return await asyncio.wait_for(
                    fn(**arguments), self.app_config().plugins.call_timeout
                )
            except (PluginError, TypeError) as e:
                # Expected failures (bad arguments, feature off): report, don't count.
                if isinstance(e, NeedsAuth):
                    await self._fail(record, tool_name, e, False)
                raise
            except TimeoutError as e:
                await self._fail(record, tool_name, e, False)
                raise PluginError(f"{tool_name} timed out") from None
            except Exception as e:
                await self._fail(record, tool_name, e, False)
                raise

        return handler

    # -- sync and webhooks ------------------------------------------------------------

    async def _sync_loop(self, record: PluginRecord, instance: Plugin) -> None:
        interval = record.manifest.sync_interval or 3600
        while record.instance is instance and record.status == "running":
            await self.sync_now(record.manifest.name)
            await asyncio.sleep(interval)

    async def sync_now(self, name: str) -> bool:
        record = self.get(name)
        async with record.lock:
            instance = record.instance
            if instance is None or record.status != "running":
                return False
            ok = await self._guard(
                record, "sync", instance.sync, self.app_config().plugins.sync_timeout
            )
            if ok:
                record.last_sync_at = utcnow()
                await self._save(record)
            return ok

    def schedule_sync(self, name: str) -> None:
        task = asyncio.create_task(self.sync_now(name))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def drain(self) -> None:
        """Wait for scheduled background syncs (tests)."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    async def handle_webhook(
        self, name: str, token: str, headers: dict[str, str], body: bytes
    ) -> bool:
        """Deliver an inbound webhook. Returns False if it was not accepted."""
        record = self.records.get(name)
        if (
            record is None
            or not record.manifest.webhook
            or not token
            or not hmac.compare_digest(token.encode(), record.webhook_token.encode())
        ):
            return False
        instance = record.instance
        if instance is None or record.status != "running":
            return True  # authentic but nothing to do; don't make the sender retry
        await self._guard(
            record, "webhook", lambda: instance.handle_webhook(headers, body),
            self.app_config().plugins.call_timeout,
        )
        return True

    def notification_channels(self) -> dict[str, Callable[[Notification], Awaitable[None]]]:
        """Running plugins that can deliver notifications, by plugin name."""
        return {
            name: (lambda n, r=record: self._send_notification(r, n))
            for name, record in self.records.items()
            if record.status == "running"
            and record.instance is not None
            and "notification_channel" in record.manifest.capabilities
        }

    async def _send_notification(self, record: PluginRecord, notification: Notification) -> None:
        instance = record.instance
        if instance is None or record.status != "running":
            raise PluginError(f"plugin {record.manifest.name} is not running")
        try:
            await asyncio.wait_for(
                instance.send(notification), self.app_config().plugins.call_timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._fail(record, "send", e, False)
            raise

    def public_url(self) -> str | None:
        base = self.app_config().server.public_url or self.seen_base_url
        return base.rstrip("/") if base else None

    # -- OAuth ------------------------------------------------------------------------

    def _oauth_client(self, record: PluginRecord) -> tuple[str, str | None]:
        spec = record.manifest.oauth
        assert spec is not None
        client = self.app_config().oauth.get(spec.provider or "")
        client_id = client.client_id if client else None
        client_secret = client.client_secret if client else None
        if not client_id:
            # Plugins for other providers can declare client_id/client_secret as secrets.
            client_id = record.secrets.get("client_id")
            client_secret = record.secrets.get("client_secret")
        if not client_id:
            provider = spec.provider or "this provider"
            raise PluginError(
                f"OAuth client for {provider} is not configured "
                f"(set oauth.{spec.provider or '<provider>'}.client_id and client_secret)"
            )
        return client_id, client_secret

    def oauth_client_configured(self, record: PluginRecord) -> bool:
        try:
            self._oauth_client(record)
        except PluginError:
            return False
        return True

    def oauth_begin(self, name: str, redirect_uri: str) -> str:
        record = self.get(name)
        if record.manifest.oauth is None:
            raise PluginError(f"plugin {name} does not use OAuth")
        client_id, _ = self._oauth_client(record)
        return self.oauth.begin(
            self.user_id, name, record.manifest.oauth, client_id,
            self.required_scopes(record), redirect_uri,
        )

    async def oauth_finish(self, state: str, code: str) -> str:
        """Complete the flow started by :meth:`oauth_begin`. Returns the plugin name."""
        name = self.oauth.pending_plugin(state)
        if self.oauth.pending_user(state) != self.user_id or name is None or name not in self.records:
            raise PluginError("OAuth session expired or unknown; start the connection again")
        record = self.records[name]
        client_id, client_secret = self._oauth_client(record)
        tokens = await self.oauth.finish(
            self.user_id, state, code, record.manifest.oauth, client_id, client_secret
        )
        previous = record.secrets.get(OAUTH_KEY) or {}
        tokens["refresh_token"] = tokens.get("refresh_token") or previous.get("refresh_token")
        record.secrets[OAUTH_KEY] = tokens
        await self._save(record, secrets=True)
        if record.enabled:
            await self._start(record)
        return name

    async def oauth_disconnect(self, name: str) -> PluginRecord:
        record = self.get(name)
        record.secrets.pop(OAUTH_KEY, None)
        await self._save(record, secrets=True)
        if record.enabled:
            await self._start(record)
        return record

    async def access_token(self, record: PluginRecord, force: bool = False) -> str:
        spec = record.manifest.oauth
        if spec is None:
            raise PluginError("plugin does not declare OAuth in its manifest")
        async with record.token_lock:
            tokens = record.secrets.get(OAUTH_KEY)
            if not tokens:
                raise NeedsAuth("account not connected")
            if force or is_expired(tokens):
                client_id, client_secret = self._oauth_client(record)
                tokens = await self.oauth.refresh(tokens, spec, client_id, client_secret)
                record.secrets[OAUTH_KEY] = tokens
                await self._save(record, secrets=True)
            return tokens["access_token"]

    # -- data management --------------------------------------------------------------

    async def purge_data(self, name: str) -> int:
        """Delete everything a plugin synced, plus its cursors."""
        from sqlalchemy import delete

        removed = await self.items.delete_plugin(name)
        async with self.db.session() as s:
            await s.execute(
                delete(PluginKV).where(PluginKV.user_id == self.user_id, PluginKV.plugin == name)
            )
            await s.commit()
        return removed

    # -- presentation -----------------------------------------------------------------

    def info(self, record: PluginRecord) -> dict[str, Any]:
        m = record.manifest
        oauth = None
        if m.oauth is not None:
            tokens = record.secrets.get(OAUTH_KEY) or {}
            oauth = {
                "provider": m.oauth.provider,
                "connected": bool(tokens),
                "client_configured": self.oauth_client_configured(record),
                "requested_scopes": self.required_scopes(record),
                "granted_scopes": tokens.get("scopes", []),
            }
        base = self.public_url()
        last = record.last_sync_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return {
            "name": m.name,
            "version": m.version,
            "description": m.description,
            "builtin": record.builtin,
            "capabilities": m.capabilities,
            "permissions": m.permissions.model_dump(),
            "config_schema": m.config_schema,
            "config": _with_defaults(m.config_schema, record.config),
            "secrets": {s: bool(record.secrets.get(s)) for s in m.secrets},
            "enabled": record.enabled,
            "status": record.status,
            "error": record.error,
            "last_sync_at": last.isoformat() if last else None,
            "sync_interval": m.sync_interval,
            "tools": list(record.tool_names),
            "oauth": oauth,
            "webhook_url": (
                f"{base}/api/plugins/{m.name}/webhook?token={record.webhook_token}"
                if m.webhook and base else None
            ),
        }

    def list(self) -> list[dict[str, Any]]:
        return [self.info(r) for r in sorted(self.records.values(), key=lambda r: r.manifest.name)]


def _short(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):  # say which manifest fields are wrong
        return "invalid manifest: " + "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'manifest'}: {err['msg']}"
            for err in exc.errors()[:5]
        )
    text = str(exc).strip().splitlines()
    first = text[0] if text else ""
    return f"{type(exc).__name__}: {first}"[:300] if first else type(exc).__name__
