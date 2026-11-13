"""Hardening: second factor, sessions, first-run setup, headers, cross-origin, logs."""

from __future__ import annotations

import stat
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from assistant.api.app import create_app
from assistant.core import totp
from assistant.core.config import ConfigService
from assistant.core.models import Setting
from assistant.core.security import redact

PASSWORD = "correct horse battery"
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890" (RFC 6238 appendix B)


def fresh(app) -> httpx.AsyncClient:
    """A second browser: no cookies."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def enable_totp(client, clock) -> tuple[str, list[str]]:
    secret = (await client.post("/api/auth/totp/setup")).json()["secret"]
    r = await client.post("/api/auth/totp/enable", json={"code": totp.code_at(secret, clock())})
    assert r.status_code == 200, r.text
    return secret, r.json()["recovery_codes"]


# -- TOTP -----------------------------------------------------------------------------


def test_totp_matches_the_rfc_vectors_and_refuses_replays():
    at = lambda seconds: datetime.fromtimestamp(seconds, UTC)  # noqa: E731
    assert totp.code_at(RFC_SECRET, at(59)) == "287082"
    assert totp.code_at(RFC_SECRET, at(1111111109)) == "081804"
    assert totp.code_at(RFC_SECRET, at(20000000000)) == "353130"

    now = at(1111111109)
    counter = totp.verify(RFC_SECRET, "081 804", now)
    assert counter == 1111111109 // 30
    assert totp.verify(RFC_SECRET, "081804", at(1111111109 + 30)) == counter  # one step of drift
    assert totp.verify(RFC_SECRET, "081804", at(1111111109 + 90)) is None  # too old
    assert totp.verify(RFC_SECRET, "081804", now, after=counter) is None  # already used
    assert totp.verify(RFC_SECRET, "abcdef", now) is None and totp.verify(RFC_SECRET, "", now) is None

    codes = totp.new_recovery_codes()
    assert len(set(codes)) == 8 and all(len(c) == 11 for c in codes)
    assert totp.hash_recovery(codes[0].upper() + " ") == totp.hash_recovery(codes[0])
    assert "secret=" + RFC_SECRET in totp.provisioning_uri(RFC_SECRET, "Assistant")


async def test_two_step_sign_in(client, app, clock):
    c = app.state.c
    other = fresh(app)
    assert (await other.post("/api/auth/login", json={"password": PASSWORD})).status_code == 200

    # Enrolment only takes effect once a code from the app has been confirmed.
    secret = (await client.post("/api/auth/totp/setup")).json()["secret"]
    assert (await client.post("/api/auth/totp/enable", json={"code": "000000"})).status_code == 422
    assert (await client.get("/api/auth/security")).json()["totp_enabled"] is False
    r = await client.post("/api/auth/totp/enable", json={"code": totp.code_at(secret, clock())})
    recovery = r.json()["recovery_codes"]
    assert (await client.get("/api/auth/security")).json() == {
        "totp_enabled": True, "recovery_codes_left": 8, "sessions": 1,
    }
    # Other devices were signed out; this one stays in.
    assert (await other.get("/api/conversations")).status_code == 401
    assert (await client.get("/api/conversations")).status_code == 200

    # The secret is encrypted at rest and recovery codes are stored as hashes.
    async with c.db.session() as s:
        rows = {r.key: r for r in (await s.scalars(select(Setting))).all()}
    assert rows["auth.totp_secret"].encrypted and secret not in str(rows["auth.totp_secret"].value)
    assert not any(code in str(rows["auth.recovery_hashes"].value) for code in recovery)

    # Password alone is no longer enough.
    r = await other.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 401 and r.json()["detail"] == "code_required"
    r = await other.post("/api/auth/login", json={"password": PASSWORD, "code": "123456"})
    assert r.status_code == 401 and r.json()["detail"] == "Wrong code"
    r = await other.post("/api/auth/login", json={"password": "nope nope nope", "code": "123456"})
    assert r.json()["detail"] == "Wrong password"

    # The code used for enrolment cannot be used again; the next one works, once.
    used = totp.code_at(secret, clock())
    assert (await other.post("/api/auth/login", json={"password": PASSWORD, "code": used})).status_code == 401
    clock.advance(seconds=30)
    code = totp.code_at(secret, clock())
    assert (await other.post("/api/auth/login", json={"password": PASSWORD, "code": code})).status_code == 200
    third = fresh(app)
    assert (await third.post("/api/auth/login", json={"password": PASSWORD, "code": code})).status_code == 401

    # A recovery code works exactly once.
    c.login_guard.reset()
    assert (await third.post("/api/auth/login", json={"password": PASSWORD, "code": recovery[0].upper()})).status_code == 200
    fourth = fresh(app)
    assert (await fourth.post("/api/auth/login", json={"password": PASSWORD, "code": recovery[0]})).status_code == 401
    assert (await client.get("/api/auth/security")).json()["recovery_codes_left"] == 7

    # Turning it off, or replacing the codes, needs the password again.
    assert (await client.post("/api/auth/totp/disable", json={"password": "wrong wrong"})).status_code == 403
    new_codes = (await client.post("/api/auth/totp/recovery-codes", json={"password": PASSWORD})).json()["recovery_codes"]
    assert len(new_codes) == 8 and recovery[1] not in new_codes
    assert (await client.post("/api/auth/totp/disable", json={"password": PASSWORD})).status_code == 200
    assert (await fourth.post("/api/auth/login", json={"password": PASSWORD})).status_code == 200

    actions = [e["action"] for e in (await client.get("/api/audit")).json()]
    for expected in ("auth.totp_on", "auth.recovery_code", "auth.failed", "auth.totp_off"):
        assert expected in actions


async def test_wrong_codes_lock_sign_in(client, app, clock):
    await enable_totp(client, clock)
    other = fresh(app)
    for _ in range(5):
        r = await other.post("/api/auth/login", json={"password": PASSWORD, "code": "000000"})
        assert r.status_code == 401
    assert (await other.post("/api/auth/login", json={"password": PASSWORD, "code": "000000"})).status_code == 429
    # Asking for the code is not counted as a failure.
    app.state.c.login_guard.reset()
    for _ in range(8):
        assert (await other.post("/api/auth/login", json={"password": PASSWORD})).json()["detail"] == "code_required"


async def test_password_change_signs_out_other_devices(client, app):
    other = fresh(app)
    await other.post("/api/auth/login", json={"password": PASSWORD})
    r = await client.post("/api/auth/password", json={"current": "wrong", "new": "a brand new password"})
    assert r.status_code == 403
    r = await client.post("/api/auth/password", json={"current": PASSWORD, "new": "short"})
    assert r.status_code == 422
    r = await client.post("/api/auth/password", json={"current": PASSWORD, "new": "a brand new password"})
    assert r.status_code == 200
    assert (await other.get("/api/conversations")).status_code == 401
    assert (await client.get("/api/conversations")).status_code == 200
    assert (await other.post("/api/auth/login", json={"password": PASSWORD})).status_code == 401
    assert (await other.post("/api/auth/login", json={"password": "a brand new password"})).status_code == 200
    assert (await client.post("/api/auth/sessions/revoke-others")).status_code == 200
    assert (await other.get("/api/conversations")).status_code == 401


# -- first run ------------------------------------------------------------------------


async def test_first_run_on_a_network_address_needs_the_code_from_the_log(app_config, mock_llm, web, caplog):
    app_config["server"]["host"] = "0.0.0.0"
    application = create_app(
        ConfigService(app_config), llm_http_client=mock_llm.http_client(),
        plugin_http_client=web.client(), notify_http_client=web.client(),
        migrate=False, secret_key="test-secret",
    )
    with caplog.at_level("WARNING"):
        async with application.router.lifespan_context(application):
            code = application.state.c.setup_code
            assert code and code in caplog.text  # the owner reads it from the server log
            c = fresh(application)
            assert (await c.get("/api/auth/status")).json()["setup_code_required"] is True
            r = await c.post("/api/auth/setup", json={"password": PASSWORD})
            assert r.status_code == 403
            r = await c.post("/api/auth/setup", json={"password": PASSWORD, "setup_code": "deadbeef"})
            assert r.status_code == 403
            r = await c.post("/api/auth/setup", json={"password": PASSWORD, "setup_code": code.upper()})
            assert r.status_code == 200
            assert application.state.c.setup_code is None
            assert (await c.get("/api/auth/status")).json() == {
                "setup_required": False, "setup_code_required": False, "authenticated": True,
            }
            assert (await fresh(application).post("/api/auth/setup", json={"password": PASSWORD})).status_code == 409


# -- HTTP hardening -------------------------------------------------------------------


async def test_security_headers(client):
    for path in ("/api/health", "/api/settings", "/api/nope"):
        h = (await client.get(path)).headers
        assert "default-src 'self'" in h["content-security-policy"]
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
        assert h["referrer-policy"] == "no-referrer" and h["cache-control"] == "no-store"
        assert "strict-transport-security" not in h
    h = (await client.get("/api/health", headers={"X-Forwarded-Proto": "https"})).headers
    assert h["strict-transport-security"].startswith("max-age=")


async def test_session_cookie_flags(app):
    c = fresh(app)
    r = await c.post("/api/auth/setup", json={"password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" not in cookie
    r = await c.post("/api/auth/login", json={"password": PASSWORD}, headers={"X-Forwarded-Proto": "https"})
    assert "secure" in r.headers["set-cookie"].lower()


async def test_cross_origin_writes_are_refused(client, app):
    body = {"content": "planted", "category": "general"}
    r = await client.post("/api/memory/facts", json=body, headers={"Origin": "http://evil.test"})
    assert r.status_code == 403 and r.headers["x-frame-options"] == "DENY"
    # Same site, different port: SameSite cookies alone would have let this through.
    r = await client.post("/api/memory/facts", json=body, headers={"Origin": "http://test:8080"})
    assert r.status_code == 403
    r = await client.post("/api/memory/facts", json=body, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert await app.state.c.memory.list_facts() == []

    for origin in ("http://test", "https://assistant.test"):  # the app itself, or its public URL
        r = await client.post("/api/memory/facts", json=body, headers={"Origin": origin})
        assert r.status_code == 201
    assert (await client.post("/api/memory/facts", json=body)).status_code == 201  # scripts, curl
    assert (await client.get("/api/memory/facts", headers={"Origin": "http://evil.test"})).status_code == 200

    # Webhooks come from other servers and carry their own token: not blocked here.
    r = await client.post("/api/plugins/gmail/webhook?token=x", headers={"Origin": "http://evil.test"})
    assert r.status_code != 403 or "Cross-origin" not in r.text


# -- logs and files -------------------------------------------------------------------


@pytest.mark.parametrize("line, secret", [
    ('127.0.0.1 - "POST /api/plugins/gmail/webhook?token=AbC123xyzSECRET HTTP/1.1" 200', "AbC123xyzSECRET"),
    ('"GET /api/oauth/callback?state=st4te-value&code=4/0AfJoh-code HTTP/1.1" 303', "4/0AfJoh-code"),
    ('"GET /api/oauth/callback?state=st4te-value&code=4/0AfJoh-code HTTP/1.1" 303', "st4te-value"),
    ("Authorization: Bearer sk-live-abcdefghijklmnop", "abcdefghijklmnop"),
    ("POST https://api.telegram.org/bot123456:ABCdefGHIjklMNOpqrSTUvwxYZ/sendMessage", "ABCdefGHIjklMNOpqrSTUvwxYZ"),
    ('{"client_secret": "GOCSPX-verysecretvalue"}', "verysecretvalue"),
])
def test_secrets_are_scrubbed_from_log_lines(line, secret):
    assert secret not in redact(line) and "[REDACTED]" in redact(line)


def test_ordinary_log_lines_are_left_alone():
    line = '127.0.0.1 - "GET /api/reminders?all=true HTTP/1.1" 200'
    assert redact(line) == line


async def test_data_directory_is_private(app, tmp_path):
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
