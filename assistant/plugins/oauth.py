"""OAuth 2.0 authorization-code flow (with PKCE) and token refresh for plugins."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from .sdk import NeedsAuth, OAuthSpec, PluginError

PROVIDERS: dict[str, dict[str, Any]] = {
    "google": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        # offline + consent => Google returns a refresh token.
        "auth_params": {"access_type": "offline", "prompt": "consent"},
    }
}
STATE_TTL = 600
EXPIRY_SLACK = 60


@dataclass
class _Pending:
    user_id: int
    plugin: str
    verifier: str
    redirect_uri: str
    scopes: list[str]
    created: float


def endpoints(spec: OAuthSpec) -> tuple[str, str, dict[str, str]]:
    known = PROVIDERS.get(spec.provider or "", {})
    auth_url = spec.auth_url or known.get("auth_url")
    token_url = spec.token_url or known.get("token_url")
    if not auth_url or not token_url:
        raise PluginError(
            f"unknown OAuth provider {spec.provider!r}: set auth_url and token_url in the manifest"
        )
    return auth_url, token_url, dict(known.get("auth_params", {}))


class OAuthService:
    def __init__(self, http: httpx.AsyncClient):
        self._http = http
        self._pending: dict[str, _Pending] = {}

    def begin(
        self, user_id: int, plugin: str, spec: OAuthSpec, client_id: str, scopes: list[str],
        redirect_uri: str,
    ) -> str:
        """Return the URL to send the user's browser to."""
        auth_url, _, extra = endpoints(spec)
        now = time.monotonic()
        self._pending = {k: v for k, v in self._pending.items() if now - v.created < STATE_TTL}
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        )
        self._pending[state] = _Pending(user_id, plugin, verifier, redirect_uri, scopes, now)
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            **extra,
        }
        return f"{auth_url}?{urlencode(params)}"

    def pending_plugin(self, state: str) -> str | None:
        pending = self._pending.get(state)
        return pending.plugin if pending else None

    def pending_user(self, state: str) -> int | None:
        """Whose connection attempt this ``state`` belongs to."""
        pending = self._pending.get(state)
        return pending.user_id if pending else None

    async def finish(
        self, user_id: int, state: str, code: str, spec: OAuthSpec, client_id: str,
        client_secret: str | None,
    ) -> dict[str, Any]:
        """Exchange the authorization code. Returns the token record to store."""
        pending = self._pending.pop(state, None)
        if (
            pending is None or pending.user_id != user_id
            or time.monotonic() - pending.created > STATE_TTL
        ):
            raise PluginError("OAuth session expired or unknown; start the connection again")
        _, token_url, _ = endpoints(spec)
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending.redirect_uri,
            "client_id": client_id,
            "code_verifier": pending.verifier,
        }
        if client_secret:
            data["client_secret"] = client_secret
        payload = await self._token_request(token_url, data)
        return _record(payload, fallback_scopes=pending.scopes)

    async def refresh(
        self, tokens: dict[str, Any], spec: OAuthSpec, client_id: str, client_secret: str | None
    ) -> dict[str, Any]:
        if not tokens.get("refresh_token"):
            raise NeedsAuth("no refresh token; reconnect the account")
        _, token_url, _ = endpoints(spec)
        data = {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_id,
        }
        if client_secret:
            data["client_secret"] = client_secret
        payload = await self._token_request(token_url, data, refreshing=True)
        record = _record(payload, fallback_scopes=tokens.get("scopes", []))
        # Providers usually omit the refresh token on refresh: keep the old one.
        record["refresh_token"] = record.get("refresh_token") or tokens["refresh_token"]
        return record

    async def _token_request(
        self, url: str, data: dict[str, str], refreshing: bool = False
    ) -> dict[str, Any]:
        try:
            response = await self._http.post(url, data=data, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise PluginError(f"token endpoint unreachable: {type(e).__name__}") from e
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400 or "access_token" not in payload:
            error = payload.get("error", f"HTTP {response.status_code}")
            detail = payload.get("error_description", "")
            message = f"token request failed: {error} {detail}".strip()
            if refreshing and error in ("invalid_grant", "invalid_client", "unauthorized_client"):
                raise NeedsAuth(message + " — reconnect the account")
            raise PluginError(message)
        return payload


def _record(payload: dict[str, Any], fallback_scopes: list[str]) -> dict[str, Any]:
    scope = payload.get("scope")
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        "expires_at": time.time() + float(payload.get("expires_in", 3600)),
        "scopes": scope.split() if isinstance(scope, str) else list(fallback_scopes),
    }


def is_expired(tokens: dict[str, Any]) -> bool:
    return time.time() >= float(tokens.get("expires_at", 0)) - EXPIRY_SLACK
