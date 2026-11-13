"""Web UI authentication: single-user password + server-side sessions.

The password is set once through ``/api/auth/setup`` on first run and stored as an Argon2
hash. Sessions are random tokens in an HttpOnly, SameSite=Strict cookie; only their SHA-256
is stored. A second factor (TOTP, with one-time recovery codes) can be turned on.

On first run anyone who can reach the server could claim it by setting the password, so
when the server listens on more than loopback the setup form also needs a one-time code
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
from sqlalchemy import delete, select

from ..core import totp
from ..core.models import AuthSession, utcnow
from ..core.security import hash_password, hash_token, new_session_token, verify_password
from .hardening import is_https

log = logging.getLogger(__name__)

COOKIE = "pa_session"
PASSWORD_KEY = "auth.password_hash"
TOTP_KEY = "auth.totp_secret"
TOTP_PENDING_KEY = "auth.totp_pending"
TOTP_LAST_KEY = "auth.totp_last_counter"
RECOVERY_KEY = "auth.recovery_hashes"
LOOPBACK = ("127.0.0.1", "localhost", "::1")

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginGuard:
    """Slows down password guessing: 5 failures locks login for 15 minutes."""

    MAX_FAILURES = 5
    WINDOW = 15 * 60

    def __init__(self) -> None:
        self._failures: list[float] = []

    def check(self) -> None:
        cutoff = time.monotonic() - self.WINDOW
        self._failures = [t for t in self._failures if t > cutoff]
        if len(self._failures) >= self.MAX_FAILURES:
            raise HTTPException(429, "Too many failed logins. Try again later.")

    def fail(self) -> None:
        self._failures.append(time.monotonic())

    def reset(self) -> None:
        self._failures.clear()


class PasswordBody(BaseModel):
    password: str


class SetupBody(BaseModel):
    password: str
    setup_code: str = ""


class LoginBody(BaseModel):
    password: str
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


async def _is_authenticated(request: Request) -> bool:
    token = request.cookies.get(COOKIE)
    if not token:
        return False
    c = container(request)
    async with c.db.session() as s:
        row = await s.get(AuthSession, hash_token(token))
        if row is None:
            return False
        expires = row.expires_at
        if expires.tzinfo is None:  # SQLite drops tzinfo
            expires = expires.replace(tzinfo=utcnow().tzinfo)
        if expires < utcnow():
            await s.delete(row)
            await s.commit()
            return False
    return True


async def require_auth(request: Request) -> None:
    if not await _is_authenticated(request):
        raise HTTPException(401, "Not authenticated")


async def _start_session(request: Request, response: Response) -> None:
    c = container(request)
    cfg = c.config.config
    token = new_session_token()
    ttl = timedelta(hours=cfg.auth.session_ttl_hours)
    async with c.db.session() as s:
        await s.execute(delete(AuthSession).where(AuthSession.expires_at < utcnow()))
        s.add(AuthSession(token_hash=hash_token(token), expires_at=utcnow() + ttl))
        await s.commit()
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


@router.get("/status")
async def status(request: Request):
    c = container(request)
    setup_required = await c.settings.get(PASSWORD_KEY) is None
    return {
        "setup_required": setup_required,
        "setup_code_required": setup_required and c.setup_code is not None,
        "authenticated": await _is_authenticated(request),
    }


def _check_password_rules(request: Request, password: str) -> None:
    minimum = container(request).config.config.auth.min_password_length
    if len(password) < minimum:
        raise HTTPException(422, f"Password must be at least {minimum} characters")


@router.post("/setup")
async def setup(body: SetupBody, request: Request, response: Response):
    """Set the password. Only works while no password exists."""
    c = container(request)
    if await c.settings.get(PASSWORD_KEY) is not None:
        raise HTTPException(409, "Already set up")
    if c.setup_code is not None:
        c.login_guard.check()
        if not hmac.compare_digest(body.setup_code.strip().lower(), c.setup_code):
            c.login_guard.fail()
            raise HTTPException(403, "Wrong setup code. It is printed in the server log at startup.")
    _check_password_rules(request, body.password)
    await c.settings.set(PASSWORD_KEY, hash_password(body.password))
    c.setup_code = None
    c.login_guard.reset()
    await c.audit.record("user", "auth.setup", "Set the password for this assistant", change=True)
    await _start_session(request, response)
    return {"ok": True}


async def _second_factor_ok(c, code: str) -> bool:
    """Check an authenticator or recovery code, and use it up."""
    secret = await c.settings.get(TOTP_KEY)
    counter = totp.verify(secret, code, c.clock(), after=await c.settings.get(TOTP_LAST_KEY, -1))
    if counter is not None:
        await c.settings.set(TOTP_LAST_KEY, counter)
        return True
    hashes = list(await c.settings.get(RECOVERY_KEY, []))
    candidate = totp.hash_recovery(code)
    for stored in hashes:
        if hmac.compare_digest(stored, candidate):
            hashes.remove(stored)
            await c.settings.set(RECOVERY_KEY, hashes)
            await c.audit.record(
                "user", "auth.recovery_code", f"Signed in with a recovery code ({len(hashes)} left)",
                change=True,
            )
            return True
    return False


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    c = container(request)
    c.login_guard.check()
    hashed = await c.settings.get(PASSWORD_KEY)
    if hashed is None or not verify_password(body.password, hashed):
        c.login_guard.fail()
        await c.audit.record("unknown", "auth.failed", "Failed sign-in: wrong password")
        raise HTTPException(401, "Wrong password")
    if await c.settings.get(TOTP_KEY):
        if not body.code.strip():
            # Not a failure: the form asks for the code next.
            raise HTTPException(401, "code_required")
        if not await _second_factor_ok(c, body.code):
            c.login_guard.fail()
            await c.audit.record("unknown", "auth.failed", "Failed sign-in: wrong code")
            raise HTTPException(401, "Wrong code")
    c.login_guard.reset()
    await c.audit.record("user", "auth.login", "Signed in")
    await _start_session(request, response)
    return {"ok": True}


@router.post("/logout", dependencies=[Depends(require_auth)])
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


async def _drop_other_sessions(request: Request) -> None:
    c = container(request)
    current = hash_token(request.cookies.get(COOKIE, ""))
    async with c.db.session() as s:
        await s.execute(delete(AuthSession).where(AuthSession.token_hash != current))
        await s.commit()


async def _reauth(request: Request, password: str) -> None:
    """Sensitive changes need the password again, not just a live session."""
    c = container(request)
    c.login_guard.check()
    if not verify_password(password, await c.settings.get(PASSWORD_KEY) or ""):
        c.login_guard.fail()
        raise HTTPException(403, "Wrong password")


@authed.get("/security")
async def security(request: Request):
    c = container(request)
    async with c.db.session() as s:
        sessions = len((await s.scalars(select(AuthSession.token_hash))).all())
    return {
        "totp_enabled": bool(await c.settings.get(TOTP_KEY)),
        "recovery_codes_left": len(await c.settings.get(RECOVERY_KEY, [])),
        "sessions": sessions,
    }


@authed.post("/password")
async def change_password(body: ChangePasswordBody, request: Request):
    c = container(request)
    await _reauth(request, body.current)
    _check_password_rules(request, body.new)
    await c.settings.set(PASSWORD_KEY, hash_password(body.new))
    await _drop_other_sessions(request)
    await c.audit.record(
        "user", "auth.password", "Changed the password and signed out other devices", change=True
    )
    return {"ok": True}


@authed.post("/sessions/revoke-others")
async def revoke_others(request: Request):
    await _drop_other_sessions(request)
    await container(request).audit.record("user", "auth.sessions", "Signed out all other devices", change=True)
    return {"ok": True}


@authed.post("/totp/setup")
async def totp_setup(request: Request):
    """Start enrolment: returns the secret to put in an authenticator app. Nothing changes
    until a code from the app is confirmed with ``/totp/enable``."""
    c = container(request)
    if await c.settings.get(TOTP_KEY):
        raise HTTPException(409, "Two-step sign-in is already on")
    secret = totp.new_secret()
    await c.settings.set(TOTP_PENDING_KEY, secret, secret=True)
    return {"secret": secret, "uri": totp.provisioning_uri(secret, c.config.config.agent.name)}


async def _new_recovery_codes(c) -> list[str]:
    codes = totp.new_recovery_codes()
    await c.settings.set(RECOVERY_KEY, [totp.hash_recovery(code) for code in codes])
    return codes


@authed.post("/totp/enable")
async def totp_enable(body: CodeBody, request: Request):
    c = container(request)
    pending = await c.settings.get(TOTP_PENDING_KEY)
    if not pending or await c.settings.get(TOTP_KEY):
        raise HTTPException(409, "Start the setup first")
    counter = totp.verify(pending, body.code, c.clock())
    if counter is None:
        raise HTTPException(422, "That code is not right. Check the app and try again.")
    await c.settings.set(TOTP_KEY, pending, secret=True)
    await c.settings.set(TOTP_LAST_KEY, counter)
    await c.settings.delete(TOTP_PENDING_KEY)
    await _drop_other_sessions(request)
    await c.audit.record("user", "auth.totp_on", "Turned on two-step sign-in", change=True)
    # Shown once; only their hashes are kept.
    return {"recovery_codes": await _new_recovery_codes(c)}


@authed.post("/totp/recovery-codes")
async def totp_new_recovery_codes(body: PasswordBody, request: Request):
    c = container(request)
    await _reauth(request, body.password)
    if not await c.settings.get(TOTP_KEY):
        raise HTTPException(409, "Two-step sign-in is off")
    await c.audit.record("user", "auth.recovery_codes", "Replaced the recovery codes", change=True)
    return {"recovery_codes": await _new_recovery_codes(c)}


@authed.post("/totp/disable")
async def totp_disable(body: PasswordBody, request: Request):
    c = container(request)
    await _reauth(request, body.password)
    for key in (TOTP_KEY, TOTP_PENDING_KEY, TOTP_LAST_KEY, RECOVERY_KEY):
        await c.settings.delete(key)
    await c.audit.record("user", "auth.totp_off", "Turned off two-step sign-in", change=True)
    return {"ok": True}


router.include_router(authed)
