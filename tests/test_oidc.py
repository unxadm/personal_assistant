"""Optional single sign-on (OpenID Connect) against a fake identity provider.

The provider here is a real protocol implementation of the parts that matter: discovery,
published keys, and a token endpoint that checks the PKCE verifier and the client's
credentials and signs ID tokens with RS256. The negative tests tamper with exactly one thing
at a time, and each must end in "no session"."""

from __future__ import annotations

import base64
import hashlib
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select

from assistant.core.models import OidcIdentity, User

from .conftest import PASSWORD, add_user, browser, sign_in

ISSUER = "https://idp.test"
CLIENT_ID = "assistant"
SECRET = "s3cret-from-the-provider"
CALLBACK = "https://assistant.test/api/auth/oidc/callback"


class FakeIdP:
    def __init__(self, web):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "key-1"
        self.claims = {"sub": "idp-user-1", "email": "ann@example.com", "email_verified": True,
                       "preferred_username": "ann", "name": "Ann Example"}
        self.methods = ["client_secret_post", "client_secret_basic"]
        self.issued: dict[str, dict] = {}
        self.token_requests: list[httpx.Request] = []
        self.tamper = lambda payload, headers: None  # edit the token before it is signed
        self.sign_with = None  # a different private key
        self.token_status = 200
        self.discovery_issuer = ISSUER
        self.extra_keys: list[dict] = []
        self.published = self.jwk(self.key, self.kid)
        web.route("GET", rf"{ISSUER}/\.well-known/openid-configuration")(lambda r, m: self.discovery)
        web.route("GET", rf"{ISSUER}/jwks")(lambda r, m: {"keys": [self.published, *self.extra_keys]})
        web.route("POST", rf"{ISSUER}/token")(self.token)

    @property
    def discovery(self):
        return {
            "issuer": self.discovery_issuer, "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/jwks",
            "token_endpoint_auth_methods_supported": self.methods,
        }

    @staticmethod
    def jwk(key, kid):
        data = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
        return {**data, "kid": kid, "use": "sig", "alg": "RS256"}

    def authorize(self, location: str) -> str:
        """The provider's side of the redirect: remember the request, hand back a code."""
        query = parse_qs(urlsplit(location).query)
        assert location.startswith(f"{ISSUER}/authorize?")
        assert query["response_type"] == ["code"] and query["client_id"] == [CLIENT_ID]
        assert query["code_challenge_method"] == ["S256"] and query["redirect_uri"] == [CALLBACK]
        assert query["scope"] == ["openid profile email"]
        code = f"code-{len(self.issued) + 1}"
        self.issued[code] = {"nonce": query["nonce"][0], "challenge": query["code_challenge"][0]}
        return code

    def token(self, request, match):
        self.token_requests.append(request)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        entry = self.issued.get(form.get("code", ""))
        basic = request.headers.get("authorization", "")
        authed = (
            form.get("client_secret") == SECRET
            or basic == "Basic " + base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
        )
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        ).rstrip(b"=").decode()
        if entry is None or not authed or challenge != entry["challenge"] or form.get(
            "redirect_uri"
        ) != CALLBACK:
            return httpx.Response(400, json={"error": "invalid_grant",
                                             "error_description": "secret-detail-from-provider"})
        if self.token_status != 200:
            return httpx.Response(self.token_status, json={"error": "server_error"})
        now = int(time.time())
        payload = {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300,
                   "nonce": entry["nonce"], **self.claims}
        headers = {"kid": self.kid}
        self.tamper(payload, headers)
        key = self.sign_with or self.key
        alg = headers.pop("alg", "RS256")
        return httpx.Response(200, json={"id_token": jwt.encode(
            payload, key if alg.startswith(("RS", "PS")) else "x" * 40, algorithm=alg,
            headers=headers), "access_token": "ignored"})


@pytest.fixture
def app_config(app_config):
    app_config["auth"] = {"oidc": {
        "enabled": True, "issuer": ISSUER, "client_id": CLIENT_ID, "client_secret": SECRET,
        "label": "Company login"}}
    return app_config


@pytest.fixture
def idp(web):
    return FakeIdP(web)


def set_oidc(app, **values):
    c = app.state.c
    c.config._base["auth"]["oidc"].update(values)
    c.config.apply_overrides({})


async def begin(client, idp) -> tuple[str, str]:
    """Open the provider's sign-in page from this browser: (state, code)."""
    r = await client.get("/api/auth/oidc/login")
    assert r.status_code == 303, r.text
    location = r.headers["location"]
    return parse_qs(urlsplit(location).query)["state"][0], idp.authorize(location)


async def come_back(client, state: str, code: str) -> httpx.Response:
    return await client.get(f"/api/auth/oidc/callback?state={state}&code={code}")


async def sso(app, idp) -> tuple[httpx.AsyncClient, httpx.Response]:
    """A fresh browser going through the whole flow."""
    c = browser(app)
    state, code = await begin(c, idp)
    return c, await come_back(c, state, code)


def error_of(response: httpx.Response) -> str:
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return query.get("oidc_error", [""])[0]


async def me(client) -> dict | None:
    r = await client.get("/api/auth/me")
    return r.json() if r.status_code == 200 else None


async def link(client, idp) -> httpx.Response:
    r = await client.post("/api/auth/oidc/link")
    assert r.status_code == 200, r.text
    code = idp.authorize(r.json()["url"])
    state = parse_qs(urlsplit(r.json()["url"]).query)["state"][0]
    return await come_back(client, state, code)


async def identities(app) -> int:
    async with app.state.c.db.session() as s:
        return await s.scalar(select(func.count()).select_from(OidcIdentity)) or 0


# -- configuration and the redirect ---------------------------------------------------


async def test_the_button_is_offered_only_when_it_is_configured(app, client, idp):
    status = (await browser(app).get("/api/auth/status")).json()
    assert status["oidc"] == {"enabled": True, "label": "Company login", "password_login": True}
    set_oidc(app, enabled=False)
    assert (await browser(app).get("/api/auth/status")).json()["oidc"]["enabled"] is False
    r = await browser(app).get("/api/auth/oidc/login")
    assert "not turned on" in error_of(r)
    # No secret is ever part of what the status or the redirect says.
    assert SECRET not in (await browser(app).get("/api/auth/status")).text


async def test_the_redirect_uses_pkce_state_and_a_flow_cookie(app, idp):
    c = browser(app)
    r = await c.get("/api/auth/oidc/login")
    query = parse_qs(urlsplit(r.headers["location"]).query)
    assert {"state", "nonce", "code_challenge"} <= set(query) and SECRET not in r.headers["location"]
    cookie = r.headers["set-cookie"].lower()
    assert f"pa_oidc={query['state'][0]}".lower() in cookie and "httponly" in cookie
    assert "samesite=lax" in cookie and "path=/api/auth/oidc" in cookie
    idp.authorize(r.headers["location"])  # asserts the rest of the request


async def test_only_https_providers_are_accepted(app, idp):
    set_oidc(app, issuer="http://idp.example")
    assert "https" in error_of(await browser(app).get("/api/auth/oidc/login"))


async def test_a_provider_claiming_another_issuer_is_refused(app, idp):
    idp.discovery_issuer = "https://evil.test"
    assert "issuer" in error_of(await browser(app).get("/api/auth/oidc/login"))


# -- linking and signing in -----------------------------------------------------------


async def test_a_sign_in_needs_a_linked_identity(app, client, idp):
    browser_, r = await sso(app, idp)
    assert "No account is linked" in error_of(r)
    assert await me(browser_) is None
    # Even though the e-mail address matches the owner's? There is no owner address, but a
    # matching username must not link either.
    idp.claims.update(preferred_username="owner", sub="someone-else", email="owner@example.com")
    _, r = await sso(app, idp)
    assert "No account is linked" in error_of(r)


async def test_link_then_sign_in(app, client, idp):
    r = await link(client, idp)
    assert r.status_code == 303 and "oidc=linked" in r.headers["location"]
    assert "tab=settings" in r.headers["location"]
    security = (await client.get("/api/auth/security")).json()
    assert security["has_password"] is True
    assert security["sso"]["linked"][0]["email"] == "ann@example.com"

    browser_, r = await sso(app, idp)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert (await me(browser_))["username"] == "owner"
    log = [e["action"] for e in (await client.get("/api/audit")).json()]
    assert "auth.oidc_link" in log and "auth.oidc_login" in log
    # The session cookie keeps the usual protections.
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()


async def test_an_identity_links_to_one_account_only(app, client, idp):
    await link(client, idp)
    await add_user(app, "alice")
    alice = await sign_in(app, "alice")
    r = await link(alice, idp)
    assert "already linked" in error_of(r)
    assert await identities(app) == 1


async def test_unlinking_needs_the_password_and_a_password_to_fall_back_on(app, client, idp):
    await link(client, idp)
    assert (await client.post("/api/auth/oidc/unlink", json={"password": "nope"})).status_code == 403
    assert (await client.post("/api/auth/oidc/unlink", json={"password": PASSWORD})).status_code == 200
    assert (await client.get("/api/auth/security")).json()["sso"]["linked"] == []
    _, r = await sso(app, idp)
    assert "No account is linked" in error_of(r)


async def test_a_disabled_account_cannot_use_sso(app, client, idp):
    await add_user(app, "alice")
    alice = await sign_in(app, "alice")
    await link(alice, idp)
    await client.patch("/api/users/2", json={"disabled": True, "password": PASSWORD})
    browser_, r = await sso(app, idp)
    assert "disabled" in error_of(r) and await me(browser_) is None


async def test_deleting_an_account_removes_its_identity(app, client, idp):
    await add_user(app, "alice")
    await link(await sign_in(app, "alice"), idp)
    assert await identities(app) == 1
    await client.request("DELETE", "/api/users/2", json={"password": PASSWORD})
    assert await identities(app) == 0


# -- creating accounts ----------------------------------------------------------------


async def test_accounts_are_created_only_when_allowed(app, client, idp):
    set_oidc(app, auto_create=True)
    browser_, r = await sso(app, idp)
    assert r.headers["location"] == "/"
    user = await me(browser_)
    assert user["username"] == "ann" and user["role"] == "member" and user["display_name"] == "Ann Example"
    assert (await browser_.get("/api/auth/security")).json()["has_password"] is False
    # The account has its own, empty assistant.
    assert (await browser_.get("/api/conversations")).json() == []
    # Signing in again finds the same account instead of creating another.
    _, r = await sso(app, idp)
    assert r.headers["location"] == "/"
    async with app.state.c.db.session() as s:
        assert await s.scalar(select(func.count()).select_from(User)) == 2


async def test_a_created_account_has_no_usable_password(app, client, idp):
    set_oidc(app, auto_create=True)
    await sso(app, idp)
    for password in ("", "!", "ann", PASSWORD):
        r = await browser(app).post("/api/auth/login", json={"username": "ann", "password": password})
        assert r.status_code == 401
    # It can choose one (without a current password) after signing in through SSO.
    browser_, _ = await sso(app, idp)
    r = await browser_.post("/api/auth/password", json={"current": "", "new": "ann picks a password"})
    assert r.status_code == 200
    r = await browser(app).post("/api/auth/login", json={"username": "ann", "password": "ann picks a password"})
    assert r.status_code == 200
    # From then on changing it needs the current one.
    r = await browser_.post("/api/auth/password", json={"current": "", "new": "something else entirely"})
    assert r.status_code == 403


async def test_auto_create_wants_a_verified_address_in_an_allowed_domain(app, idp):
    set_oidc(app, auto_create=True, allowed_email_domains=["example.com"])
    idp.claims.update(email_verified=False)
    assert "not allowed" in error_of((await sso(app, idp))[1])
    idp.claims.update(email_verified="true", email="ann@elsewhere.test")
    assert "not allowed" in error_of((await sso(app, idp))[1])
    idp.claims.pop("email")
    assert "not allowed" in error_of((await sso(app, idp))[1])
    idp.claims.update(email="ann@Example.com")
    assert (await sso(app, idp))[1].headers["location"] == "/"


async def test_created_usernames_are_cleaned_and_made_unique(app, client, idp):
    set_oidc(app, auto_create=True)
    await add_user(app, "ann")
    idp.claims.update(preferred_username="Ann!! Example")
    browser_, _ = await sso(app, idp)
    assert (await me(browser_))["username"] == "annexample"
    idp.claims.update(sub="two", preferred_username="ann")
    browser_, _ = await sso(app, idp)
    assert (await me(browser_))["username"] == "ann2"


async def test_the_default_role_applies_to_created_accounts(app, idp):
    set_oidc(app, auto_create=True, default_role="admin")
    browser_, _ = await sso(app, idp)
    assert (await me(browser_))["role"] == "admin"


async def test_nothing_is_created_before_the_first_account_exists(app, idp):
    set_oidc(app, auto_create=True)
    async with app.state.c.db.session() as s:
        await s.execute(User.__table__.delete())
        await s.commit()
    assert "No account is linked" in error_of((await sso(app, idp))[1])


# -- the browser binding ----------------------------------------------------------------


async def test_a_flow_only_completes_in_the_browser_that_started_it(app, client, idp):
    await link(client, idp)
    victim = browser(app)
    attacker = browser(app)
    state, code = await begin(attacker, idp)
    # The attacker lures the victim's browser to the callback with the attacker's state.
    assert "did not start in this browser" in error_of(await come_back(victim, state, code))
    assert await me(victim) is None
    # A missing or wrong cookie fails the same way.
    state, code = await begin(attacker, idp)
    attacker.cookies.clear()
    assert "did not start" in error_of(await come_back(attacker, state, code))
    state, code = await begin(attacker, idp)
    attacker.cookies.set("pa_oidc", "wrong", path="/api/auth/oidc")
    assert "did not start" in error_of(await come_back(attacker, state, code))


async def test_a_flow_cannot_be_replayed_and_unknown_state_is_refused(app, client, idp):
    await link(client, idp)
    c = browser(app)
    state, code = await begin(c, idp)
    first = await come_back(c, state, code)
    assert first.headers["location"] == "/"
    again = browser(app)
    again.cookies.set("pa_oidc", state, path="/api/auth/oidc")
    assert "did not start" in error_of(await come_back(again, state, code))
    assert "did not start" in error_of(await come_back(c, "unknown", code))


async def test_provider_errors_and_missing_codes_end_in_a_message(app, idp):
    c = browser(app)
    r = await c.get("/api/auth/oidc/callback?error=access_denied&state=x")
    assert "cancelled" in error_of(r)
    r = await c.get("/api/auth/oidc/callback")
    assert "cancelled" in error_of(r)


# -- what is checked on the token -----------------------------------------------------


@pytest.mark.parametrize("name, tamper", [
    ("another audience", lambda p, h: p.update(aud="someone-else")),
    ("another issuer", lambda p, h: p.update(iss="https://evil.test")),
    ("expired", lambda p, h: p.update(exp=int(time.time()) - 600)),
    ("issued in the future", lambda p, h: p.update(iat=int(time.time()) + 3600)),
    ("another nonce", lambda p, h: p.update(nonce="not-mine")),
    ("no nonce", lambda p, h: p.pop("nonce")),
    ("no subject", lambda p, h: p.pop("sub")),
    ("no expiry", lambda p, h: p.pop("exp")),
    ("several audiences, not ours as authorised party", lambda p, h: p.update(aud=[CLIENT_ID, "x"])),
    ("an unknown key id", lambda p, h: h.update(kid="other")),
    ("an unsupported algorithm", lambda p, h: h.update(alg="HS256")),
])
async def test_tokens_that_do_not_check_out_never_sign_anyone_in(app, client, idp, name, tamper):
    await link(client, idp)
    idp.tamper = tamper
    browser_, r = await sso(app, idp)
    assert error_of(r), name
    assert await me(browser_) is None, name


async def test_a_token_signed_with_another_key_is_refused(app, client, idp):
    await link(client, idp)
    idp.sign_with = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    browser_, r = await sso(app, idp)
    assert "could not be verified" in error_of(r) and await me(browser_) is None


async def test_an_unsigned_token_is_refused(app, client, idp):
    await link(client, idp)

    def unsigned(payload, headers):
        raise AssertionError  # replaced below

    original = idp.token

    def token(request, match):
        response = original(request, match)
        if response.status_code != 200:
            return response
        body = response.json()
        claims = jwt.decode(body["id_token"], options={"verify_signature": False})
        body["id_token"] = jwt.encode(claims, None, algorithm="none")
        return httpx.Response(200, json=body)

    app.state.c.oidc._http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: token(r, None) if r.url.path == "/token" else _passthrough(idp, r)))
    c = browser(app)
    state, code = await begin(c, idp)
    r = await come_back(c, state, code)
    assert error_of(r) and await me(c) is None


def _passthrough(idp, request):
    if request.url.path == "/jwks":
        return httpx.Response(200, json={"keys": [idp.published]})
    return httpx.Response(200, json=idp.discovery)


async def test_a_rotated_signing_key_is_picked_up(app, client, idp):
    await link(client, idp)
    await sso(app, idp)  # the old key is now cached
    idp.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    idp.kid = "key-2"
    idp.published = idp.jwk(idp.key, idp.kid)
    browser_, r = await sso(app, idp)
    assert r.headers["location"] == "/" and (await me(browser_))["username"] == "owner"


async def test_the_provider_s_own_error_text_is_not_shown(app, client, idp, caplog):
    await link(client, idp)
    idp.token_status = 500
    c = browser(app)
    state, code = await begin(c, idp)
    # A wrong code makes the fake provider answer with an error description.
    r = await come_back(c, state, "wrong-code")
    message = error_of(r)
    assert "did not accept" in message and "secret-detail" not in message
    assert SECRET not in caplog.text and "secret-detail" not in r.headers["location"]


async def test_the_client_secret_goes_where_the_provider_asks(app, client, idp):
    await link(client, idp)
    await sso(app, idp)
    request = idp.token_requests[-1]
    assert f"client_secret={SECRET}" in request.content.decode()  # client_secret_post offered
    assert "authorization" not in request.headers

    idp.methods = ["client_secret_basic"]
    app.state.c.oidc._discovery = None
    await sso(app, idp)
    request = idp.token_requests[-1]
    assert SECRET not in request.content.decode() and request.headers["authorization"].startswith("Basic ")


# -- password sign-in alongside -------------------------------------------------------


async def test_password_sign_in_can_be_turned_off(app, client, idp):
    await link(client, idp)
    set_oidc(app, password_login=False)
    status = (await browser(app).get("/api/auth/status")).json()
    assert status["oidc"]["password_login"] is False
    r = await browser(app).post("/api/auth/login", json={"username": "owner", "password": PASSWORD})
    assert r.status_code == 403
    browser_, r = await sso(app, idp)
    assert (await me(browser_))["username"] == "owner"
    # With SSO itself off the password form is back, whatever the option says.
    set_oidc(app, enabled=False)
    assert (await browser(app).post(
        "/api/auth/login", json={"username": "owner", "password": PASSWORD})).status_code == 200


async def test_sso_users_cannot_unlink_without_a_password(app, idp):
    set_oidc(app, auto_create=True)
    browser_, _ = await sso(app, idp)
    r = await browser_.post("/api/auth/oidc/unlink", json={"password": "x"})
    assert r.status_code == 409 and "Set a password first" in r.json()["detail"]


async def test_two_step_sign_in_is_the_providers_job_for_sso(app, client, idp, clock):
    from assistant.core import totp

    await link(client, idp)
    secret = (await client.post("/api/auth/totp/setup")).json()["secret"]
    await client.post("/api/auth/totp/enable", json={"code": totp.code_at(secret, clock())})
    # The password still needs the code; the provider's own sign-in does not ask for it twice.
    r = await browser(app).post("/api/auth/login", json={"username": "owner", "password": PASSWORD})
    assert r.json()["detail"] == "code_required"
    browser_, r = await sso(app, idp)
    assert (await me(browser_))["username"] == "owner"


async def test_the_callback_is_reachable_without_a_session_but_link_and_unlink_are_not(app, idp):
    anon = browser(app)
    assert (await anon.post("/api/auth/oidc/link")).status_code == 401
    assert (await anon.post("/api/auth/oidc/unlink", json={"password": "x"})).status_code == 401
    assert (await anon.get("/api/auth/oidc/callback")).status_code == 303


# -- the Android app: a one-time code instead of a cookie -------------------------------

APP = "assistant.client://sso"


def pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(b"v" * 32).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


async def app_sign_in(app, idp, challenge: str) -> tuple[httpx.AsyncClient, httpx.Response]:
    """The phone's browser doing the flow the app started."""
    phone = browser(app)
    r = await phone.get("/api/auth/oidc/login", params={"challenge": challenge})
    assert r.status_code == 303, r.text
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    return phone, await come_back(phone, state, idp.authorize(r.headers["location"]))


def app_result(response: httpx.Response) -> dict[str, str]:
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(APP + "?"), location
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


async def linked(app, client, idp) -> None:
    assert (await link(client, idp)).status_code == 303


async def test_the_app_trades_a_one_time_code_for_a_session(app, client, idp):
    await linked(app, client, idp)
    verifier, challenge = pkce()
    phone, r = await app_sign_in(app, idp, challenge)
    result = app_result(r)
    assert set(result) == {"code"}
    # The browser got no session: the app gets it, from the exchange.
    assert "pa_session" not in r.headers.get("set-cookie", "")
    assert await me(phone) is None
    app_client = browser(app)
    r = await app_client.post("/api/auth/oidc/exchange", json={"code": result["code"], "verifier": verifier})
    assert r.status_code == 200 and r.json()["user"]["username"] == "owner"
    assert "pa_session=" in r.headers["set-cookie"]
    assert (await me(app_client))["username"] == "owner"


async def test_a_code_works_once(app, client, idp):
    await linked(app, client, idp)
    verifier, challenge = pkce()
    code = app_result((await app_sign_in(app, idp, challenge))[1])["code"]
    body = {"code": code, "verifier": verifier}
    assert (await browser(app).post("/api/auth/oidc/exchange", json=body)).status_code == 200
    assert (await browser(app).post("/api/auth/oidc/exchange", json=body)).status_code == 401


async def test_an_app_that_only_saw_the_redirect_cannot_use_the_code(app, client, idp):
    """Any app can register the same URL scheme and read the code; the verifier is what counts."""
    await linked(app, client, idp)
    verifier, challenge = pkce()
    code = app_result((await app_sign_in(app, idp, challenge))[1])["code"]
    thief = browser(app)
    for guess in ("", "x" * 43, base64.urlsafe_b64encode(b"w" * 32).rstrip(b"=").decode(), challenge):
        r = await thief.post("/api/auth/oidc/exchange", json={"code": code, "verifier": guess})
        assert r.status_code == 401
    # The failed attempts used the code up, so the real app has to start again.
    r = await browser(app).post("/api/auth/oidc/exchange", json={"code": code, "verifier": verifier})
    assert r.status_code == 401


async def test_a_code_expires(app, client, idp, monkeypatch):
    await linked(app, client, idp)
    verifier, challenge = pkce()
    code = app_result((await app_sign_in(app, idp, challenge))[1])["code"]
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + 61)
    r = await browser(app).post("/api/auth/oidc/exchange", json={"code": code, "verifier": verifier})
    assert r.status_code == 401


async def test_a_made_up_code_is_refused(app, client, idp):
    verifier, _ = pkce()
    r = await browser(app).post("/api/auth/oidc/exchange", json={"code": "nope", "verifier": verifier})
    assert r.status_code == 401 and "expired" in r.json()["detail"]


async def test_errors_go_back_to_the_app_and_say_nothing_about_the_provider(app, idp):
    _, challenge = pkce()
    # No linked account: the same refusal as in the browser, delivered to the app.
    phone, r = await app_sign_in(app, idp, challenge)
    assert "No account is linked" in app_result(r)["error"]
    # The provider turned the person away.
    r = await phone.get("/api/auth/oidc/login", params={"challenge": challenge})
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    r = await phone.get(f"/api/auth/oidc/callback?state={state}&error=access_denied&error_description=secret")
    assert "secret" not in r.headers["location"] and "refused" in app_result(r)["error"]
    # A tampered token.
    idp.tamper = lambda payload, headers: payload.update(aud="someone-else")
    _, r = await app_sign_in(app, idp, challenge)
    assert "could not be verified" in app_result(r)["error"]


async def test_a_badly_formed_challenge_is_refused(app, idp):
    for challenge in ("short", "!" * 43, "a" * 44):
        r = await browser(app).get("/api/auth/oidc/login", params={"challenge": challenge})
        assert r.status_code == 303 and r.headers["location"].startswith(APP + "?error=")
    assert not idp.issued


async def test_the_app_flow_respects_disabled_accounts(app, client, idp):
    await linked(app, client, idp)
    async with app.state.c.db.session() as s:
        (await s.get(User, 1)).disabled = True
        await s.commit()
    _, challenge = pkce()
    _, r = await app_sign_in(app, idp, challenge)
    assert "disabled" in app_result(r)["error"]


async def test_a_code_for_an_account_disabled_later_is_refused(app, client, idp):
    await linked(app, client, idp)
    verifier, challenge = pkce()
    code = app_result((await app_sign_in(app, idp, challenge))[1])["code"]
    async with app.state.c.db.session() as s:
        (await s.get(User, 1)).disabled = True
        await s.commit()
    r = await browser(app).post("/api/auth/oidc/exchange", json={"code": code, "verifier": verifier})
    assert r.status_code == 401


async def test_the_app_can_create_an_account_when_that_is_allowed(app, idp):
    set_oidc(app, auto_create=True)
    verifier, challenge = pkce()
    code = app_result((await app_sign_in(app, idp, challenge))[1])["code"]
    app_client = browser(app)
    r = await app_client.post("/api/auth/oidc/exchange", json={"code": code, "verifier": verifier})
    assert r.status_code == 200 and (await me(app_client))["username"] == "ann"


async def test_a_browser_flow_is_not_turned_into_an_app_flow(app, client, idp):
    """Only the request that started the flow decides where the result goes."""
    await linked(app, client, idp)
    phone = browser(app)
    state, code = await begin(phone, idp)
    r = await come_back(phone, state, code)
    assert r.headers["location"] == "/" and "pa_session=" in r.headers["set-cookie"]
