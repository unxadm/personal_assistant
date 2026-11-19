"""OpenID Connect sign-in (optional): authorization-code flow with PKCE.

The provider proves who someone is; it does not create access on its own. A sign-in only
succeeds for an identity (issuer + subject) that was linked to an account, or, when the
admin allows it, creates one for a verified e-mail address. E-mail addresses and usernames
are never used to match an existing account: a provider that lets people choose unverified
addresses could otherwise be used to take over any account.

What is checked on the ID token: signature against the provider's published keys (only
asymmetric algorithms, so "none" and key-confusion tricks are refused), issuer, audience
(and ``azp``), expiry, issued-at and the ``nonce`` of this very sign-in. ``state`` is bound
to the browser that started the flow through a cookie, so a finished flow cannot be pushed
into someone else's browser.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

from .config import AppConfig, OidcConfig

log = logging.getLogger(__name__)

STATE_TTL = 600
HANDOFF_TTL = 60
DISCOVERY_TTL = 3600
ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"]
LOOPBACK = ("localhost", "127.0.0.1", "::1")


class OidcError(Exception):
    """A sign-in that cannot proceed. The message is safe to show to the person."""


@dataclass
class Flow:
    verifier: str
    nonce: str
    redirect_uri: str
    # Set when a signed-in user is linking their account instead of signing in.
    user_id: int | None
    created: float
    # Set when the Android app started the flow: the S256 challenge of a secret only the app
    # holds. The browser then hands the app a one-time code instead of a session.
    app_challenge: str | None = None


@dataclass
class Handoff:
    user_id: int
    challenge: str
    created: float


@dataclass
class Claims:
    issuer: str
    subject: str
    email: str | None
    email_verified: bool
    username: str | None
    name: str | None


def _secure(url: str, what: str) -> None:
    parts = urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK):
        return
    raise OidcError(f"The {what} must use https.")


class OidcService:
    def __init__(self, get_config: Callable[[], AppConfig], http: httpx.AsyncClient):
        self._get_config = get_config
        self._http = http
        self._flows: dict[str, Flow] = {}
        self._handoffs: dict[str, Handoff] = {}
        self._discovery: tuple[float, dict[str, Any]] | None = None
        self._jwks: tuple[float, dict[str, Any]] | None = None

    @property
    def cfg(self) -> OidcConfig:
        return self._get_config().auth.oidc

    @property
    def enabled(self) -> bool:
        c = self.cfg
        return bool(c.enabled and c.issuer and c.client_id)

    def redirect_uri(self, base_url: str) -> str:
        return self.cfg.redirect_uri or f"{base_url.rstrip('/')}/api/auth/oidc/callback"

    # -- provider metadata ------------------------------------------------------------

    async def _get_json(self, url: str) -> dict[str, Any]:
        _secure(url, "provider address")
        try:
            response = await self._http.get(url, timeout=10.0, follow_redirects=False)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("OIDC provider request failed: %s", type(e).__name__)
            raise OidcError("The sign-in provider could not be reached.") from None
        if not isinstance(data, dict):
            raise OidcError("The sign-in provider answered with something unexpected.")
        return data

    async def discovery(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._discovery and now - self._discovery[0] < DISCOVERY_TTL:
            return self._discovery[1]
        issuer = (self.cfg.issuer or "").rstrip("/")
        doc = await self._get_json(f"{issuer}/.well-known/openid-configuration")
        if str(doc.get("issuer", "")).rstrip("/") != issuer:
            raise OidcError("The provider's issuer does not match the configured one.")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not doc.get(key):
                raise OidcError("The provider's configuration is incomplete.")
            _secure(str(doc[key]), "provider's endpoints")
        self._discovery = (now, doc)
        return doc

    async def _keys(self, *, refresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not refresh and self._jwks and now - self._jwks[0] < DISCOVERY_TTL:
            return self._jwks[1]
        jwks = await self._get_json(str((await self.discovery())["jwks_uri"]))
        self._jwks = (now, jwks)
        return jwks

    # -- the flow ---------------------------------------------------------------------

    async def begin(
        self, redirect_uri: str, user_id: int | None = None, app_challenge: str | None = None
    ) -> tuple[str, str]:
        """Start a sign-in (or a link for ``user_id``). Returns (provider URL, state)."""
        if not self.enabled:
            raise OidcError("Single sign-on is not turned on.")
        doc = await self.discovery()
        now = time.monotonic()
        self._flows = {k: v for k, v in self._flows.items() if now - v.created < STATE_TTL}
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        )
        self._flows[state] = Flow(verifier, nonce, redirect_uri, user_id, now, app_challenge)
        params = {
            "response_type": "code",
            "client_id": self.cfg.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.cfg.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{doc['authorization_endpoint']}?{urlencode(params)}", state

    def peek_app(self, state: str) -> bool:
        """Whether this state belongs to a sign-in started by the app (does not consume it)."""
        flow = self._flows.get(state)
        return flow is not None and flow.app_challenge is not None

    # -- hand-off to the app ----------------------------------------------------------
    # The app cannot share the browser's cookies, and a custom-scheme redirect can be seen by
    # any app that registered the same scheme. So the browser hands over a short-lived,
    # single-use code that is useless without the verifier only the app knows.

    def issue_handoff(self, user_id: int, challenge: str) -> str:
        now = time.monotonic()
        self._handoffs = {k: v for k, v in self._handoffs.items() if now - v.created < HANDOFF_TTL}
        code = secrets.token_urlsafe(32)
        self._handoffs[code] = Handoff(user_id, challenge, now)
        return code

    def redeem_handoff(self, code: str, verifier: str) -> int | None:
        """The user id for a valid code + verifier, once. A wrong verifier burns the code."""
        handoff = self._handoffs.pop(code, None)
        if handoff is None or time.monotonic() - handoff.created > HANDOFF_TTL:
            return None
        if not 43 <= len(verifier) <= 128:
            return None
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        if not secrets.compare_digest(expected.rstrip(b"=").decode(), handoff.challenge):
            return None
        return handoff.user_id

    async def finish(self, state: str, code: str, cookie_state: str | None) -> tuple[Flow, Claims]:
        """Complete the flow: returns it and the verified claims."""
        flow = self._flows.pop(state, None)
        if (
            flow is None
            or time.monotonic() - flow.created > STATE_TTL
            or not cookie_state
            or not secrets.compare_digest(cookie_state, state)
        ):
            raise OidcError("This sign-in expired or did not start in this browser. Try again.")
        doc = await self.discovery()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": flow.redirect_uri,
            "code_verifier": flow.verifier,
        }
        auth = None
        methods = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        if not self.cfg.client_secret:
            data["client_id"] = self.cfg.client_id or ""
        elif "client_secret_post" in methods:
            data |= {"client_id": self.cfg.client_id or "", "client_secret": self.cfg.client_secret}
        else:
            auth = httpx.BasicAuth(self.cfg.client_id or "", self.cfg.client_secret)
        try:
            response = await self._http.post(
                str(doc["token_endpoint"]), data=data, auth=auth or httpx.USE_CLIENT_DEFAULT,
                timeout=15.0, follow_redirects=False,
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("OIDC token request failed: %s", type(e).__name__)
            raise OidcError("The sign-in provider could not be reached.") from None
        if response.status_code >= 400 or not isinstance(payload, dict) or not payload.get("id_token"):
            # The provider's own error text is not shown: it is not ours to vouch for.
            log.warning("OIDC token endpoint refused the code (HTTP %s)", response.status_code)
            raise OidcError("The sign-in provider did not accept the sign-in.")
        return flow, await self._verify(str(payload["id_token"]), flow.nonce)

    async def _verify(self, token: str, nonce: str) -> Claims:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise OidcError("The sign-in provider sent an invalid token.") from None
        alg = header.get("alg")
        if alg not in ALGORITHMS:
            raise OidcError("The sign-in provider's token uses an unsupported signature.")
        key = await self._signing_key(header.get("kid"))
        issuer = (self.cfg.issuer or "").rstrip("/")
        try:
            claims = jwt.decode(
                token, key, algorithms=[alg], audience=self.cfg.client_id,
                issuer=[issuer, issuer + "/"], leeway=60,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as e:
            log.warning("OIDC ID token rejected: %s", type(e).__name__)
            raise OidcError("The sign-in provider's token could not be verified.") from None
        audience = claims.get("aud")
        if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != self.cfg.client_id:
            raise OidcError("The sign-in provider's token is not meant for this app.")
        if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise OidcError("The sign-in provider's token does not belong to this sign-in.")
        subject = str(claims["sub"])
        if not subject or len(subject) > 255:
            raise OidcError("The sign-in provider sent an invalid identity.")
        email = claims.get("email")
        return Claims(
            issuer=issuer, subject=subject,
            email=str(email)[:320] if isinstance(email, str) and email else None,
            # Some providers send "true" as a string.
            email_verified=claims.get("email_verified") in (True, "true"),
            username=str(claims["preferred_username"]) if claims.get("preferred_username") else None,
            name=str(claims["name"]) if claims.get("name") else None,
        )

    async def _signing_key(self, kid: str | None):
        for refresh in (False, True):  # keys rotate: look again once before giving up
            try:
                keys = jwt.PyJWKSet.from_dict(await self._keys(refresh=refresh)).keys
            except (jwt.PyJWTError, ValueError, KeyError):
                raise OidcError("The sign-in provider's keys could not be read.") from None
            matches = [k for k in keys if kid is None or k.key_id == kid]
            if len(matches) == 1 or (kid is None and matches and len(keys) == 1):
                return matches[0].key
        raise OidcError("The token was signed with a key the provider does not publish.")
