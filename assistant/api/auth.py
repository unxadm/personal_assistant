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
import secrets
import time
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from ..core import totp
from ..core.models import AuthSession, utcnow
from ..core.security import hash_password, hash_token, new_session_token, verify_password
from ..core.users import CurrentUser, UserError
from .hardening import is_https

log = logging.getLogger(__name__)

COOKIE = "pa_session"
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
    return {
        "totp_enabled": bool(await settings.get(TOTP_KEY)),
        "recovery_codes_left": len(await settings.get(RECOVERY_KEY, [])),
        "sessions": sessions or 0,
    }


@bootstrap.post("/password")
async def change_password(
    body: ChangePasswordBody, request: Request, user: CurrentUser = Depends(require_session)
):
    c = container(request)
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


router.include_router(authed)
router.include_router(bootstrap)
