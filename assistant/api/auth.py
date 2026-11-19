"""Web UI authentication: accounts with a password and server-side sessions.

The first account is created through ``/api/auth/setup`` on first run and is an admin; more
are added by admins (``/api/users``). Passwords are Argon2 hashes. Sessions are random tokens
in an HttpOnly, SameSite=Strict cookie; only their SHA-256 is stored, next to the account
they belong to. A second factor (TOTP, with one-time recovery codes) can be turned on per
account.

On first run anyone who can reach the server could claim it by creating the first account,
so when the server listens on more than loopback the setup form also needs a one-time code
that is printed in the server log.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
import time
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from ..core import totp
from ..core.models import AuthSession, OidcIdentity, utcnow
from ..core.oidc import Claims, OidcError
from ..core.security import hash_password, hash_token, new_session_token, verify_password
from ..core.users import NO_PASSWORD, CurrentUser, UserError
from .hardening import is_https

log = logging.getLogger(__name__)

COOKIE = "pa_session"
# Holds the OIDC ``state`` of the sign-in this browser started. SameSite=Lax, not Strict:
# the provider sends the browser back from another site and the cookie must come along.
OIDC_COOKIE = "pa_oidc"
# Where the browser sends the Android app after single sign-on (see ``/oidc/exchange``).
APP_REDIRECT = "assistant.client://sso"
CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
# Per-user settings (stored under the account's own scope).
TOTP_KEY = "auth.totp_secret"
TOTP_PENDING_KEY = "auth.totp_pending"
TOTP_LAST_KEY = "auth.totp_last_counter"
RECOVERY_KEY = "auth.recovery_hashes"
TOTP_KEYS = (TOTP_KEY, TOTP_PENDING_KEY, TOTP_LAST_KEY, RECOVERY_KEY)
LOOPBACK = ("127.0.0.1", "localhost", "::1")
DEFAULT_OWNER = "owner"
# Hashed once so that an unknown username costs the same time as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_hex(8))

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginGuard:
    """Slows down password guessing: 5 failures for one username lock that username for 15
    minutes, and a flood of failures across all names slows everyone's sign-in down a little
    rather than letting one attacker lock every account."""

    MAX_FAILURES = 5
    MAX_ANYWHERE = 100
    WINDOW = 15 * 60
    MAX_KEYS = 1000

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        cutoff = time.monotonic() - self.WINDOW
        recent = [t for t in self._failures.get(key, []) if t > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def check(self, key: str = "") -> None:
        if len(self._recent(key)) >= self.MAX_FAILURES:
            raise HTTPException(429, "Too many failed logins. Try again later.")
        everyone = sum(len(self._recent(k)) for k in list(self._failures))
        if everyone >= self.MAX_ANYWHERE:
            raise HTTPException(429, "Too many failed logins. Try again later.")

    def fail(self, key: str = "") -> None:
        if key not in self._failures and len(self._failures) >= self.MAX_KEYS:
            self._failures.pop(next(iter(self._failures)))
        self._failures.setdefault(key, []).append(time.monotonic())

    def reset(self, key: str = "") -> None:
        self._failures.pop(key, None)


class PasswordBody(BaseModel):
    password: str


class SetupBody(BaseModel):
    password: str
    username: str = DEFAULT_OWNER
    setup_code: str = ""


class HandoffBody(BaseModel):
    code: str = Field(max_length=200)
    verifier: str = Field(max_length=200)


class LoginBody(BaseModel):
    password: str
    # Optional while there is only one account (older clients do not send it).
    username: str = ""
    # Six digits from the authenticator app, or a recovery code.
    code: str = ""


class CodeBody(BaseModel):
    code: str


class ChangePasswordBody(BaseModel):
    current: str
    new: str


def new_setup_code(host: str) -> str | None:
    """The one-time code the first-run form needs, or None when only loopback can reach us."""
    return None if host in LOOPBACK else secrets.token_hex(4)


def container(request: Request):
    return request.app.state.c


def mine(request: Request):
    """The signed-in user's own services (their reminders, memory, plugins, ...). Only
    meaningful after :func:`require_auth` has run for the request."""
    return container(request).of(request.state.user.id)


async def _session_user(request: Request) -> CurrentUser | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    c = container(request)
    async with c.db.session() as s:
        row = await s.get(AuthSession, hash_token(token))
        if row is None:
            return None
        expires = row.expires_at
        if expires.tzinfo is None:  # SQLite drops tzinfo
            expires = expires.replace(tzinfo=utcnow().tzinfo)
        if expires < utcnow():
            await s.delete(row)
            await s.commit()
            return None
        user = await c.users.get(row.user_id)
    if user is None or user.disabled:
        return None
    return CurrentUser.of(user)


async def require_session(request: Request) -> CurrentUser:
    """Signed in. Also allowed while an admin-set password still has to be changed."""
    user = await _session_user(request)
    if user is None:
        raise HTTPException(401, "Not authenticated")
    request.state.user = user
    return user


async def require_auth(request: Request) -> CurrentUser:
    """Signed in, and past the forced password change."""
    user = await require_session(request)
    if user.must_change_password:
        raise HTTPException(403, "password_change_required")
    return user


async def require_admin(user: CurrentUser = Depends(require_auth)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(403, "Only an admin can do that")
    return user


async def _start_session(request: Request, response: Response, user_id: int) -> None:
    c = container(request)
    cfg = c.config.config
    token = new_session_token()
    ttl = timedelta(hours=cfg.auth.session_ttl_hours)
    async with c.db.session() as s:
        await s.execute(delete(AuthSession).where(AuthSession.expires_at < utcnow()))
        s.add(AuthSession(token_hash=hash_token(token), user_id=user_id, expires_at=utcnow() + ttl))
        await s.commit()
    await c.users.touch_login(user_id)
    response.set_cookie(
        COOKIE,
        token,
        max_age=int(ttl.total_seconds()),
        httponly=True,
        samesite="strict",
        # Secure whenever this request came in over HTTPS (directly or via a proxy).
        secure=cfg.server.cookie_secure or is_https(request),
        path="/",
    )


async def drop_sessions(c, user_id: int, *, keep: str | None = None) -> None:
    """Sign a user out everywhere (optionally keeping the session with this token hash)."""
    stmt = delete(AuthSession).where(AuthSession.user_id == user_id)
    if keep is not None:
        stmt = stmt.where(AuthSession.token_hash != keep)
    async with c.db.session() as s:
        await s.execute(stmt)
        await s.commit()


@router.get("/status")
async def status(request: Request):
    c = container(request)
    setup_required = await c.users.count() == 0
    user = await _session_user(request)
    return {
        "setup_required": setup_required,
        "setup_code_required": setup_required and c.setup_code is not None,
        "authenticated": user is not None,
        "user": user.view() if user else None,
        "oidc": {
            "enabled": c.oidc.enabled, "label": c.oidc.cfg.label,
            "password_login": c.oidc.cfg.password_login or not c.oidc.enabled,
        },
    }


@router.get("/me")
async def me(user: CurrentUser = Depends(require_session)):
    return user.view()


@router.post("/setup")
async def setup(body: SetupBody, request: Request, response: Response):
    """Create the first account (an admin). Only works while there are no accounts."""
    c = container(request)
    if await c.users.count() > 0:
        raise HTTPException(409, "Already set up")
    if c.setup_code is not None:
        c.login_guard.check("setup")
        if not hmac.compare_digest(body.setup_code.strip().lower(), c.setup_code):
            c.login_guard.fail("setup")
            raise HTTPException(403, "Wrong setup code. It is printed in the server log at startup.")
    try:
        user = await c.users.create(
            body.username or DEFAULT_OWNER, body.password, role="admin",
            display_name=(body.username or DEFAULT_OWNER).capitalize(),
        )
    except UserError as e:
        raise HTTPException(422, str(e)) from None
    await c.tenants.add(user)
    c.setup_code = None
    c.login_guard.reset("setup")
    await c.of(user.id).audit.record(
        "user", "auth.setup", "Created the first account for this assistant", change=True
    )
    await _start_session(request, response, user.id)
    return {"ok": True}


async def _second_factor_ok(c, user_id: int, code: str) -> bool:
    """Check an authenticator or recovery code of this account, and use it up."""
    settings = c.of(user_id).settings
    secret = await settings.get(TOTP_KEY)
    counter = totp.verify(secret, code, c.clock(), after=await settings.get(TOTP_LAST_KEY, -1))
    if counter is not None:
        await settings.set(TOTP_LAST_KEY, counter)
        return True
    hashes = list(await settings.get(RECOVERY_KEY, []))
    candidate = totp.hash_recovery(code)
    for stored in hashes:
        if hmac.compare_digest(stored, candidate):
            hashes.remove(stored)
            await settings.set(RECOVERY_KEY, hashes)
            await c.of(user_id).audit.record(
                "user", "auth.recovery_code", f"Signed in with a recovery code ({len(hashes)} left)",
                change=True,
            )
            return True
    return False


async def _unknown_failure(c, key: str, what: str) -> None:
    c.login_guard.fail(key)
    log.info("failed sign-in (%s)", what)


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    c = container(request)
    if c.oidc.enabled and not c.oidc.cfg.password_login:
        raise HTTPException(403, "Password sign-in is turned off here. Use single sign-on.")
    name = body.username.strip().lower()
    user = await c.users.by_username(name) if name else await c.users.only_user()
    guard_key = name or (user.username if user else "")
    c.login_guard.check(guard_key)
    ok = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)
    if user is None or user.disabled or not ok:
        await _unknown_failure(c, guard_key, "wrong name or password")
        if user is not None and not user.disabled:
            await c.of(user.id).audit.record("unknown", "auth.failed", "Failed sign-in: wrong password")
        # Same answer for an unknown name, a disabled account and a wrong password.
        raise HTTPException(401, "Wrong username or password")
    if await c.of(user.id).settings.get(TOTP_KEY):
        if not body.code.strip():
            # Not a failure: the form asks for the code next.
            raise HTTPException(401, "code_required")
        if not await _second_factor_ok(c, user.id, body.code):
            c.login_guard.fail(guard_key)
            await c.of(user.id).audit.record("unknown", "auth.failed", "Failed sign-in: wrong code")
            raise HTTPException(401, "Wrong code")
    c.login_guard.reset(guard_key)
    await c.of(user.id).audit.record("user", "auth.login", "Signed in")
    await _start_session(request, response, user.id)
    return {"ok": True, "user": CurrentUser.of(user).view()}


@router.post("/logout")
async def logout(request: Request, response: Response):
    c = container(request)
    token = request.cookies.get(COOKIE)
    if token:
        async with c.db.session() as s:
            await s.execute(delete(AuthSession).where(AuthSession.token_hash == hash_token(token)))
            await s.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


# -- account security (signed in) -----------------------------------------------------

authed = APIRouter(dependencies=[Depends(require_auth)])
# Reachable while an admin-set password is still waiting to be changed.
bootstrap = APIRouter()


async def _reauth(request: Request, user: CurrentUser, password: str) -> None:
    """Sensitive changes need the password again, not just a live session."""
    c = container(request)
    c.login_guard.check(user.username)
    row = await c.users.get(user.id)
    if row is None or not verify_password(password, row.password_hash):
        c.login_guard.fail(user.username)
        raise HTTPException(403, "Wrong password")


@authed.get("/security")
async def security(request: Request, user: CurrentUser = Depends(require_auth)):
    c = container(request)
    settings = c.of(user.id).settings
    async with c.db.session() as s:
        sessions = await s.scalar(
            select(func.count()).select_from(AuthSession).where(AuthSession.user_id == user.id)
        )
        identities = (
            await s.scalars(
                select(OidcIdentity).where(OidcIdentity.user_id == user.id).order_by(OidcIdentity.id)
            )
        ).all()
    row = await c.users.get(user.id)
    return {
        "totp_enabled": bool(await settings.get(TOTP_KEY)),
        "recovery_codes_left": len(await settings.get(RECOVERY_KEY, [])),
        "sessions": sessions or 0,
        "has_password": row is not None and row.password_hash != NO_PASSWORD,
        "sso": {
            "enabled": c.oidc.enabled,
            "label": c.oidc.cfg.label,
            "linked": [
                {"email": i.email, "issuer": i.issuer, "linked_at": i.created_at.isoformat()}
                for i in identities
            ],
        },
    }


@bootstrap.post("/password")
async def change_password(
    body: ChangePasswordBody, request: Request, user: CurrentUser = Depends(require_session)
):
    c = container(request)
    row = await c.users.get(user.id)
    # Someone who signed in through SSO and has no password yet sets their first one.
    if row is None or row.password_hash != NO_PASSWORD:
        await _reauth(request, user, body.current)
    try:
        await c.users.set_password(user.id, body.new)
    except UserError as e:
        raise HTTPException(422, str(e)) from None
    await drop_sessions(c, user.id, keep=hash_token(request.cookies.get(COOKIE, "")))
    await c.of(user.id).audit.record(
        "user", "auth.password", "Changed the password and signed out other devices", change=True
    )
    return {"ok": True}


@authed.post("/sessions/revoke-others")
async def revoke_others(request: Request, user: CurrentUser = Depends(require_auth)):
    c = container(request)
    await drop_sessions(c, user.id, keep=hash_token(request.cookies.get(COOKIE, "")))
    await c.of(user.id).audit.record("user", "auth.sessions", "Signed out all other devices", change=True)
    return {"ok": True}


@authed.post("/totp/setup")
async def totp_setup(request: Request, user: CurrentUser = Depends(require_auth)):
    """Start enrolment: returns the secret to put in an authenticator app. Nothing changes
    until a code from the app is confirmed with ``/totp/enable``."""
    c = container(request)
    settings = c.of(user.id).settings
    if await settings.get(TOTP_KEY):
        raise HTTPException(409, "Two-step sign-in is already on")
    secret = totp.new_secret()
    await settings.set(TOTP_PENDING_KEY, secret, secret=True)
    return {
        "secret": secret,
        "uri": totp.provisioning_uri(secret, f"{c.config.config.agent.name}:{user.username}"),
    }


async def _new_recovery_codes(settings) -> list[str]:
    codes = totp.new_recovery_codes()
    await settings.set(RECOVERY_KEY, [totp.hash_recovery(code) for code in codes])
    return codes


@authed.post("/totp/enable")
async def totp_enable(body: CodeBody, request: Request, user: CurrentUser = Depends(require_auth)):
    c = container(request)
    settings = c.of(user.id).settings
    pending = await settings.get(TOTP_PENDING_KEY)
    if not pending or await settings.get(TOTP_KEY):
        raise HTTPException(409, "Start the setup first")
    counter = totp.verify(pending, body.code, c.clock())
    if counter is None:
        raise HTTPException(422, "That code is not right. Check the app and try again.")
    await settings.set(TOTP_KEY, pending, secret=True)
    await settings.set(TOTP_LAST_KEY, counter)
    await settings.delete(TOTP_PENDING_KEY)
    await drop_sessions(c, user.id, keep=hash_token(request.cookies.get(COOKIE, "")))
    await c.of(user.id).audit.record("user", "auth.totp_on", "Turned on two-step sign-in", change=True)
    # Shown once; only their hashes are kept.
    return {"recovery_codes": await _new_recovery_codes(settings)}


@authed.post("/totp/recovery-codes")
async def totp_new_recovery_codes(
    body: PasswordBody, request: Request, user: CurrentUser = Depends(require_auth)
):
    c = container(request)
    await _reauth(request, user, body.password)
    settings = c.of(user.id).settings
    if not await settings.get(TOTP_KEY):
        raise HTTPException(409, "Two-step sign-in is off")
    await c.of(user.id).audit.record("user", "auth.recovery_codes", "Replaced the recovery codes", change=True)
    return {"recovery_codes": await _new_recovery_codes(settings)}


@authed.post("/totp/disable")
async def totp_disable(body: PasswordBody, request: Request, user: CurrentUser = Depends(require_auth)):
    c = container(request)
    await _reauth(request, user, body.password)
    for key in TOTP_KEYS:
        await c.of(user.id).settings.delete(key)
    await c.of(user.id).audit.record("user", "auth.totp_off", "Turned off two-step sign-in", change=True)
    return {"ok": True}


# -- single sign-on (OIDC) -----------------------------------------------------------


def _base_url(request: Request) -> str:
    return container(request).config.config.server.public_url or str(request.base_url)


def _flow_cookie(request: Request, response: Response, state: str) -> None:
    response.set_cookie(
        OIDC_COOKIE, state, max_age=600, httponly=True, samesite="lax", path="/api/auth/oidc",
        secure=container(request).config.config.server.cookie_secure or is_https(request),
    )


def _back(message: str = "", *, tab: str = "", linked: bool = False) -> RedirectResponse:
    query = {}
    if tab:
        query["tab"] = tab
    if linked:
        query["oidc"] = "linked"
    if message:
        query["oidc_error"] = message
    response = RedirectResponse("/?" + urlencode(query) if query else "/", status_code=303)
    response.delete_cookie(OIDC_COOKIE, path="/api/auth/oidc")
    return response


def _to_app(**query: str) -> RedirectResponse:
    response = RedirectResponse(f"{APP_REDIRECT}?{urlencode(query)}", status_code=303)
    response.delete_cookie(OIDC_COOKIE, path="/api/auth/oidc")
    return response


@router.get("/oidc/login", include_in_schema=False)
async def oidc_login(request: Request, challenge: str = ""):
    """Send the browser to the provider. With ``challenge`` the Android app is asking: the
    outcome then goes back to the app instead of the web page."""
    c = container(request)
    if challenge and not CHALLENGE.fullmatch(challenge):
        return _to_app(error="The app sent an invalid sign-in request.")
    try:
        url, state = await c.oidc.begin(
            c.oidc.redirect_uri(_base_url(request)), app_challenge=challenge or None
        )
    except OidcError as e:
        return _to_app(error=str(e)) if challenge else _back(str(e))
    response = RedirectResponse(url, status_code=303)
    _flow_cookie(request, response, state)
    return response


@authed.post("/oidc/link")
async def oidc_link(request: Request, response: Response, user: CurrentUser = Depends(require_auth)):
    """Start linking the signed-in account to a single sign-on identity."""
    c = container(request)
    try:
        url, state = await c.oidc.begin(c.oidc.redirect_uri(_base_url(request)), user.id)
    except OidcError as e:
        raise HTTPException(409, str(e)) from None
    _flow_cookie(request, response, state)
    return {"url": url}


@authed.post("/oidc/unlink")
async def oidc_unlink(body: PasswordBody, request: Request, user: CurrentUser = Depends(require_auth)):
    c = container(request)
    row = await c.users.get(user.id)
    if row is None or row.password_hash == NO_PASSWORD:
        raise HTTPException(409, "Set a password first, or you would be locked out")
    await _reauth(request, user, body.password)
    async with c.db.session() as s:
        await s.execute(delete(OidcIdentity).where(OidcIdentity.user_id == user.id))
        await s.commit()
    await c.of(user.id).audit.record("user", "auth.oidc_unlink", "Unlinked single sign-on", change=True)
    return {"ok": True}


def _username_for(claims: Claims) -> str:
    base = claims.username or (claims.email or "").split("@")[0] or "user"
    return re.sub(r"[^a-z0-9._-]", "", base.lower()).lstrip("._-")[:28] or "user"


async def _create_account(c, claims: Claims):
    """An account for a first-time SSO sign-in, if the admin allowed that."""
    cfg = c.oidc.cfg
    if not cfg.auto_create or await c.users.count() == 0:
        raise OidcError(
            "No account is linked to this sign-in. Sign in with your password and link it "
            "under Settings, or ask an admin to add you."
        )
    domain = (claims.email or "").rpartition("@")[2].lower()
    allowed = [d.lower().lstrip("@") for d in cfg.allowed_email_domains]
    if not claims.email or not claims.email_verified or (allowed and domain not in allowed):
        raise OidcError("This sign-in is not allowed to create an account here.")
    base, n = _username_for(claims), 0
    while True:
        name = base if n == 0 else f"{base}{n + 1}"
        if await c.users.by_username(name) is None:
            break
        n += 1
    user = await c.users.create(name, None, role=cfg.default_role, display_name=claims.name or name)
    await c.tenants.add(user)
    await c.of(user.id).audit.record(
        "user", "auth.oidc_create", "Account created by single sign-on", change=True
    )
    return user


@router.get("/oidc/callback", include_in_schema=False)
async def oidc_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    """Where the provider sends the browser back. The session cookie is SameSite=Strict, so
    it is absent here; the flow's own cookie and unguessable ``state`` are the proof that
    this browser started it."""
    c = container(request)
    app = c.oidc.peek_app(state)

    def bail(message: str):
        return _to_app(error=message) if app else _back(message)

    if error or not code or not state:
        return bail("The sign-in was cancelled or refused by the provider.")
    try:
        flow, claims = await c.oidc.finish(state, code, request.cookies.get(OIDC_COOKIE))
    except OidcError as e:
        return bail(str(e))
    tab = "settings" if flow.user_id is not None else ""

    async with c.db.session() as s:
        identity = await s.scalar(
            select(OidcIdentity).where(
                OidcIdentity.issuer == claims.issuer, OidcIdentity.subject == claims.subject
            )
        )
        if flow.user_id is not None:  # linking
            if identity is not None:
                return _back("That sign-in identity is already linked to an account.", tab=tab)
            target = await c.users.get(flow.user_id)
            if target is None or target.disabled:
                return _back("That account is not available.", tab=tab)
            s.add(OidcIdentity(
                user_id=target.id, issuer=claims.issuer, subject=claims.subject, email=claims.email,
            ))
            await s.commit()
            await c.of(target.id).audit.record(
                "user", "auth.oidc_link", "Linked single sign-on", change=True
            )
            return _back(tab=tab, linked=True)
        user = await c.users.get(identity.user_id) if identity is not None else None

    if user is None:
        try:
            user = await _create_account(c, claims)
        except (OidcError, UserError) as e:
            return bail(str(e))
        async with c.db.session() as s:
            s.add(OidcIdentity(
                user_id=user.id, issuer=claims.issuer, subject=claims.subject, email=claims.email,
            ))
            await s.commit()
    if user.disabled:
        await c.of(user.id).audit.record("unknown", "auth.failed", "Failed sign-in: account disabled")
        return bail("This account is disabled.")
    async with c.db.session() as s:
        row = await s.scalar(
            select(OidcIdentity).where(
                OidcIdentity.issuer == claims.issuer, OidcIdentity.subject == claims.subject
            )
        )
        if row is not None:
            row.last_login_at = utcnow()
            await s.commit()
    await c.of(user.id).audit.record("user", "auth.oidc_login", "Signed in with single sign-on")
    if flow.app_challenge:
        return _to_app(code=c.oidc.issue_handoff(user.id, flow.app_challenge))
    response = _back()
    await _start_session(request, response, user.id)
    return response


@router.post("/oidc/exchange")
async def oidc_exchange(body: HandoffBody, request: Request, response: Response):
    """The app trades the one-time code from the browser, plus its verifier, for a session.
    It answers like ``/login`` so the app handles both the same way."""
    c = container(request)
    user_id = c.oidc.redeem_handoff(body.code, body.verifier)
    user = await c.users.get(user_id) if user_id is not None else None
    if user is None or user.disabled:
        raise HTTPException(401, "This sign-in expired. Try again.")
    await _start_session(request, response, user.id)
    return {"ok": True, "user": CurrentUser.of(user).view()}


router.include_router(authed)
router.include_router(bootstrap)
