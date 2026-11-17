"""REST API: plugin manager, OAuth callback, webhooks, pending actions."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ..plugins.sdk import PluginError
from .auth import container, require_auth

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")
authed = APIRouter(dependencies=[Depends(require_auth)])


def _manager(request: Request):
    manager = container(request).plugins
    if not manager.app_config().server.public_url:
        # Remember how the browser reaches us, for webhook and redirect URLs.
        manager.seen_base_url = str(request.base_url).rstrip("/")
    return manager


def _record(request: Request, name: str):
    manager = _manager(request)
    try:
        return manager, manager.get(name)
    except KeyError:
        raise HTTPException(404, f"Unknown plugin {name!r}") from None


# -- plugin manager -------------------------------------------------------------------


@authed.get("/plugins")
async def list_plugins(request: Request):
    manager = _manager(request)
    return {"plugins": manager.list(), "invalid": manager.invalid}


@authed.post("/plugins/rescan")
async def rescan(request: Request):
    """Pick up plugins added to, changed in, or removed from the plugin directories."""
    manager = _manager(request)
    await manager.discover()
    return {"plugins": manager.list(), "invalid": manager.invalid}


@authed.post("/plugins/{name}/enable")
async def enable(name: str, request: Request):
    manager, _ = _record(request, name)
    return manager.info(await manager.enable(name))


@authed.post("/plugins/{name}/disable")
async def disable(name: str, request: Request):
    manager, _ = _record(request, name)
    return manager.info(await manager.disable(name))


class ConfigBody(BaseModel):
    config: dict[str, Any] | None = None
    # name -> value; null or "" removes the secret; omitted names are unchanged.
    secrets: dict[str, str | None] | None = None


@authed.put("/plugins/{name}/config")
async def configure(name: str, body: ConfigBody, request: Request):
    manager, _ = _record(request, name)
    try:
        return manager.info(await manager.configure(name, body.config, body.secrets))
    except PluginError as e:
        raise HTTPException(422, str(e)) from None


@authed.post("/plugins/{name}/sync")
async def sync(name: str, request: Request):
    manager, record = _record(request, name)
    if record.status != "running":
        raise HTTPException(409, "Plugin is not running")
    ok = await manager.sync_now(name)
    return {"ok": ok, **manager.info(record)}


@authed.delete("/plugins/{name}/data")
async def purge(name: str, request: Request):
    """Delete everything the plugin has synced (items, embeddings, cursors)."""
    manager, _ = _record(request, name)
    return {"removed": await manager.purge_data(name)}


# -- OAuth ----------------------------------------------------------------------------


def _redirect_uri(manager) -> str:
    return f"{manager.public_url()}/api/oauth/callback"


@authed.get("/plugins/{name}/oauth/start")
async def oauth_start(name: str, request: Request):
    manager, _ = _record(request, name)
    try:
        return {"url": manager.oauth_begin(name, _redirect_uri(manager)),
                "redirect_uri": _redirect_uri(manager)}
    except PluginError as e:
        raise HTTPException(422, str(e)) from None


@authed.post("/plugins/{name}/oauth/disconnect")
async def oauth_disconnect(name: str, request: Request):
    manager, _ = _record(request, name)
    return manager.info(await manager.oauth_disconnect(name))


@router.get("/oauth/callback", include_in_schema=False)
async def oauth_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    """Where the provider sends the browser back. Not behind the session cookie (it is
    SameSite=Strict, so it is absent on this cross-site redirect); the unguessable ``state``
    issued to the signed-in user is the proof that this flow is ours."""
    manager = container(request).plugins
    plugin = manager.oauth.pending_plugin(state) or ""
    result: dict[str, str] = {"tab": "plugins", "plugin": plugin}
    if error or not code:
        result |= {"oauth": "error", "message": error or "no authorization code returned"}
    else:
        try:
            result["plugin"] = await manager.oauth_finish(state, code)
            result["oauth"] = "connected"
        except PluginError as e:
            result |= {"oauth": "error", "message": str(e)}
        except Exception:
            log.exception("OAuth callback failed")
            result |= {"oauth": "error", "message": "unexpected error; see server log"}
    return RedirectResponse(f"/?{urlencode(result)}", status_code=303)


# -- webhooks -------------------------------------------------------------------------


@router.post("/plugins/{name}/webhook", include_in_schema=False)
async def webhook(name: str, request: Request, token: str = ""):
    """Inbound webhooks (Gmail Pub/Sub push, Calendar channels, ...). Authenticated by the
    plugin's secret token: ``?token=`` or the ``X-Goog-Channel-Token`` header."""
    token = token or request.headers.get("x-goog-channel-token", "")
    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(413, "Payload too large")
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not await container(request).plugins.handle_webhook(name, token, headers, body):
        raise HTTPException(404, "Not found")
    return {"ok": True}


# -- pending actions ------------------------------------------------------------------


@authed.get("/actions")
async def list_actions(request: Request, status: str | None = None, conversation_id: int | None = None):
    return await container(request).actions.list(status, conversation_id)


async def _resolve(request: Request, action_id: int, approve: bool):
    result = await container(request).actions.resolve(action_id, approve)
    if result is None:
        raise HTTPException(409, "Action not found or already resolved")
    return result


@authed.post("/actions/{action_id}/approve")
async def approve_action(action_id: int, request: Request):
    return await _resolve(request, action_id, True)


@authed.post("/actions/{action_id}/reject")
async def reject_action(action_id: int, request: Request):
    return await _resolve(request, action_id, False)


# -- MCP servers ----------------------------------------------------------------------
# Read-only apart from reconnecting: servers are added in config.yaml, because a server
# can be a program that runs on this machine.


@authed.get("/mcp")
async def list_mcp(request: Request):
    return {"servers": container(request).mcp.status()}


@authed.post("/mcp/{name}/reconnect")
async def reconnect_mcp(name: str, request: Request):
    manager = container(request).mcp
    if name not in {s["name"] for s in manager.status()}:
        raise HTTPException(404, f"Unknown MCP server {name!r}")
    try:
        await manager.reconnect(name)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return {"servers": manager.status()}


router.include_router(authed)
