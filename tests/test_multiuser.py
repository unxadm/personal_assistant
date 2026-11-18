"""Several people on one server: what each can see, what only admins can do, and the
account lifecycle. The point of most of these tests is a negative one: user B never sees,
changes or is affected by what belongs to user A."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from assistant.agent.tools import Tool
from assistant.core.models import (
    Attachment,
    AuditEntry,
    Conversation,
    Embedding,
    MemoryFact,
    Reminder,
    Setting,
    User,
)
from assistant.plugins.sdk import Item

from .conftest import PASSWORD, TELEGRAM, add_user, browser, sign_in, tool_call, write_plugin

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360"
    "00000002000100ffff03000006000557bfabd40000000049454e44ae426082"
)


@pytest.fixture
def app_config(app_config):
    # The Telegram chat in config.yaml is the owner's.
    app_config["notifications"] = {"telegram": dict(TELEGRAM)}
    return app_config


@pytest.fixture
async def alice(app):
    """The second account, signed in."""
    await add_user(app, "alice")
    c = await sign_in(app, "alice")
    yield c
    await c.aclose()


async def count(app, model, **where) -> int:
    async with app.state.c.db.session() as s:
        stmt = select(func.count()).select_from(model)
        for key, value in where.items():
            stmt = stmt.where(getattr(model, key) == value)
        return await s.scalar(stmt) or 0


# -- accounts: creating them, signing in, forced password change ----------------------


async def test_admin_creates_an_account_that_must_change_its_password(client, app):
    r = await client.post("/api/users", json={"username": "Bob", "display_name": "Bob B"})
    assert r.status_code == 201, r.text
    body = r.json()
    temp = body["temporary_password"]
    assert body["user"]["username"] == "bob" and body["user"]["role"] == "member" and temp
    assert body["user"]["must_change_password"] is True

    bob = browser(app)
    r = await bob.post("/api/auth/login", json={"username": "bob", "password": temp})
    assert r.status_code == 200 and r.json()["user"]["must_change_password"] is True
    # Until the password is changed, nothing but the change itself works.
    assert (await bob.get("/api/conversations")).json() == {"detail": "password_change_required"}
    assert (await bob.get("/api/auth/status")).json()["user"]["must_change_password"] is True
    r = await bob.post("/api/auth/password", json={"current": temp, "new": "short"})
    assert r.status_code == 422
    r = await bob.post("/api/auth/password", json={"current": temp, "new": "a brand new password"})
    assert r.status_code == 200
    assert (await bob.get("/api/conversations")).status_code == 200
    # The temporary one no longer works.
    other = browser(app)
    assert (await other.post(
        "/api/auth/login", json={"username": "bob", "password": temp})).status_code == 401


async def test_usernames_are_validated_and_unique(client):
    for bad in ("", "has space", "-leading", "x" * 33, "ünï"):
        assert (await client.post("/api/users", json={"username": bad})).status_code == 422
    assert (await client.post("/api/users", json={"username": "carol", "password": "short"})).status_code == 422
    assert (await client.post("/api/users", json={"username": "carol"})).status_code == 201
    assert (await client.post("/api/users", json={"username": "CAROL"})).status_code == 422


async def test_login_needs_the_username_once_there_are_several_accounts(app, client):
    only = browser(app)
    # One account: older clients that send no username still work.
    assert (await only.post("/api/auth/login", json={"password": PASSWORD})).status_code == 200
    await add_user(app, "alice")
    anyone = browser(app)
    assert (await anyone.post("/api/auth/login", json={"password": PASSWORD})).status_code == 401
    assert (await anyone.post(
        "/api/auth/login", json={"username": "alice", "password": PASSWORD})).status_code == 200


async def test_failed_logins_lock_one_username_not_everyone(app, alice):
    anyone = browser(app)
    for _ in range(5):
        r = await anyone.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
        assert r.status_code == 401
    # Alice's name is locked, even with the right password ...
    r = await anyone.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 429
    # ... but the owner can still sign in, and an attacker cannot lock them out this way.
    r = await anyone.post("/api/auth/login", json={"username": "owner", "password": PASSWORD})
    assert r.status_code == 200
    # An unknown name gets the same answer as a wrong password.
    r = await browser(app).post("/api/auth/login", json={"username": "ghost", "password": "x"})
    assert r.status_code == 401 and r.json()["detail"] == "Wrong username or password"


async def test_two_step_sign_in_is_per_account(app, client, alice, clock):
    from assistant.core import totp

    secret = (await alice.post("/api/auth/totp/setup")).json()["secret"]
    r = await alice.post("/api/auth/totp/enable", json={"code": totp.code_at(secret, clock())})
    assert r.status_code == 200
    assert (await client.get("/api/auth/security")).json()["totp_enabled"] is False
    assert (await alice.get("/api/auth/security")).json()["totp_enabled"] is True
    # The owner still signs in with the password alone.
    assert (await browser(app).post(
        "/api/auth/login", json={"username": "owner", "password": PASSWORD})).status_code == 200
    r = await browser(app).post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.json()["detail"] == "code_required"


async def test_sessions_belong_to_one_account(app, client, alice):
    assert (await client.get("/api/auth/me")).json()["username"] == "owner"
    assert (await alice.get("/api/auth/me")).json()["username"] == "alice"
    # Alice changing her password signs out her other devices, not the owner's.
    second = await sign_in(app, "alice")
    r = await alice.post("/api/auth/password", json={"current": PASSWORD, "new": "another good password"})
    assert r.status_code == 200
    assert (await second.get("/api/conversations")).status_code == 401
    assert (await client.get("/api/conversations")).status_code == 200


# -- isolation ------------------------------------------------------------------------


async def test_conversations_are_private(client, alice, mock_llm):
    mock_llm.script = ["hello owner"]
    r = await client.post("/api/chat", json={"text": "my secret plan"})
    conv_id = r.json()["conversation_id"]

    assert (await alice.get("/api/conversations")).json() == []
    assert (await alice.get(f"/api/conversations/{conv_id}/messages")).status_code == 404
    assert (await alice.delete(f"/api/conversations/{conv_id}")).status_code == 404
    # Continuing someone else's conversation is refused too, not silently allowed.
    r = await alice.post("/api/chat", json={"text": "hi", "conversation_id": conv_id})
    assert r.status_code == 404
    assert (await client.get(f"/api/conversations/{conv_id}/messages")).status_code == 200


async def test_the_model_only_sees_the_asking_users_memory(client, alice, mock_llm):
    await client.post("/api/memory/facts", json={"content": "The owner's safe code is 1234"})
    await alice.post("/api/memory/facts", json={"content": "Alice is allergic to nuts"})
    mock_llm.script = ["ok"]
    await alice.post("/api/chat", json={"text": "what do you know about me?"})
    system = mock_llm.last_chat["messages"][0]["content"]
    assert "allergic to nuts" in system and "safe code" not in system
    assert "Alice" in system  # the prompt names the user it is talking to

    assert [f["content"] for f in (await client.get("/api/memory/facts")).json()] == [
        "The owner's safe code is 1234"]
    fact_id = (await client.get("/api/memory/facts")).json()[0]["id"]
    assert (await alice.patch(f"/api/memory/facts/{fact_id}", json={"content": "x"})).status_code == 404
    assert (await alice.delete(f"/api/memory/facts/{fact_id}")).status_code == 404
    assert [f["content"] for f in (await client.get("/api/memory/facts")).json()] == [
        "The owner's safe code is 1234"]


async def test_semantic_search_never_crosses_users(app, mock_llm):
    await add_user(app, "alice")
    c = app.state.c
    await c.of(1).memory.add_fact("the owner keeps the spare key under the blue pot")
    assert await c.of(2).vectors.search("spare key under the pot", k=5) == []
    hits = await c.of(1).vectors.search("spare key under the pot", k=5)
    assert hits and "blue pot" in hits[0].text
    # Same text for both: each has their own embedding row, and deleting one keeps the other.
    await c.of(2).memory.add_fact("the owner keeps the spare key under the blue pot")
    assert await count(app, Embedding, kind="fact") == 2
    await c.of(2).vectors.delete("fact", ["1", "2"])
    assert await count(app, Embedding, user_id=1, kind="fact") == 1


async def test_items_are_per_user_even_with_the_same_external_id(app):
    await add_user(app, "alice")
    c = app.state.c
    mail = lambda who: Item(external_id="m1", type="message", title=f"for {who}", body="hi")  # noqa: E731
    await c.of(1).items.upsert("gmail", [mail("owner")])
    await c.of(2).items.upsert("gmail", [mail("alice")])
    assert [i["title"] for i in await c.of(1).items.query()] == ["for owner"]
    assert [i["title"] for i in await c.of(2).items.query()] == ["for alice"]
    await c.of(2).items.delete("gmail", ["m1"])
    assert [i["title"] for i in await c.of(1).items.query()] == ["for owner"]
    # Each user's privacy rules apply to their own data only.
    await c.of(2).items.upsert("gmail", [mail("alice")])
    c.of(2).set_overrides({"privacy.exclude_senders": ["x@y.test"]})
    assert await c.of(2).items.apply_privacy() == 0


async def test_reminders_notifications_and_the_scheduler_are_per_user(client, alice, app, clock):
    r = await client.post("/api/reminders", json={"text": "owner thing", "at": "2026-03-10T13:00"})
    assert r.status_code == 201
    rid = r.json()["id"]
    await alice.post("/api/reminders", json={"text": "alice thing", "at": "2026-03-10T13:30"})

    assert [x["text"] for x in (await alice.get("/api/reminders")).json()] == ["alice thing"]
    for call in (
        alice.patch(f"/api/reminders/{rid}", json={"text": "hijacked"}),
        alice.post(f"/api/reminders/{rid}/complete"),
        alice.delete(f"/api/reminders/{rid}"),
        alice.post(f"/api/reminders/{rid}/snooze", json={}),
    ):
        assert (await call).status_code == 404
    assert await count(app, Reminder, text="owner thing") == 1

    clock.advance(hours=1, minutes=15)  # only the owner's is due
    await app.state.c.scheduler.tick()
    assert [n["title"] for n in (await client.get("/api/notifications")).json()] == ["owner thing"]
    assert (await alice.get("/api/notifications")).json() == []
    clock.advance(minutes=30)
    await app.state.c.scheduler.tick()
    assert [n["title"] for n in (await alice.get("/api/notifications")).json()] == ["alice thing"]


async def test_notification_buttons_only_act_on_your_own_reminders(client, alice):
    rid = (await client.post("/api/reminders", json={"text": "owner thing", "at": "2026-03-10T13:00"})).json()["id"]
    r = await alice.post("/api/notifications/action", json={"ref": f"reminder:{rid}", "action": "done"})
    assert "Couldn't do that" in r.json()["message"]
    assert (await client.get("/api/reminders")).json()[0]["status"] == "scheduled"


async def test_pending_actions_and_the_audit_log_are_private(client, alice, app, mock_llm):
    async def post(ctx, text):
        return {"posted": text}

    c = app.state.c
    c.of(1).tools.register(Tool(
        "post_note", "Post", {"type": "object", "properties": {"text": {"type": "string"}}},
        post, side_effecting=True, confirm=True,
    ))
    mock_llm.script = [tool_call("post_note", '{"text": "hello"}'), "waiting"]
    r = await client.post("/api/chat", json={"text": "post it"})
    action_id = r.json()["message"]["meta"]["tool_events"][0]["pending_action_id"]

    assert (await alice.get("/api/actions")).json() == []
    assert (await alice.post(f"/api/actions/{action_id}/approve")).status_code == 409
    assert (await alice.post(f"/api/actions/{action_id}/reject")).status_code == 409
    assert [a["id"] for a in (await client.get("/api/actions?status=pending")).json()] == [action_id]
    assert (await client.post(f"/api/actions/{action_id}/approve")).status_code == 200

    owner_log = (await client.get("/api/audit")).json()
    assert any(e["action"] == "tool.post_note" for e in owner_log)
    alice_log = (await alice.get("/api/audit")).json()
    assert not any("post_note" in e["action"] for e in alice_log)
    entry = next(e for e in owner_log if e["action"] == "tool.post_note")
    assert (await alice.post(f"/api/audit/{entry['id']}/undo")).status_code == 404


async def test_tools_are_registered_per_user(app, client, alice, mock_llm):
    async def hello(ctx):
        return "hi"

    c = app.state.c
    c.of(1).tools.register(Tool("owner_only", "x", {"type": "object"}, hello))
    names = lambda t: {s["function"]["name"] for s in t.tools.specs()}  # noqa: E731
    assert "owner_only" in names(c.of(1)) and "owner_only" not in names(c.of(2))
    assert {"remember_fact", "create_reminder"} <= names(c.of(2))
    # Server-wide tools (MCP) are shared by everyone through the parent registry.
    c.tools.register(Tool("shared_tool", "x", {"type": "object"}, hello))
    assert "shared_tool" in names(c.of(1)) and "shared_tool" in names(c.of(2))
    with pytest.raises(ValueError):
        c.of(2).tools.register(Tool("shared_tool", "x", {"type": "object"}, hello))


async def test_attachments_are_private_and_deleted_with_the_account(app, client, alice):
    r = await client.post("/api/attachments", content=PNG, headers={"x-filename": "a.png"})
    assert r.status_code == 201, r.text
    att = r.json()["id"]
    assert (await client.get(f"/api/attachments/{att}")).status_code == 200
    assert (await alice.get(f"/api/attachments/{att}")).status_code == 404
    # Alice cannot attach the owner's upload to her own message either.
    r = await alice.post("/api/chat", json={"text": "look", "attachment_ids": [att]})
    assert r.status_code == 422
    assert await count(app, Attachment, message_id=None) == 1


async def test_push_devices_follow_the_account_that_subscribed(client, alice):
    sub = {
        "endpoint": "https://push.example/abc",
        "keys": {
            "p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM",
            "auth": "tBHItJI5svbpez7KI4CCXg",
        },
    }
    assert (await client.post("/api/push/subscribe", json=sub)).status_code == 200
    assert len((await client.get("/api/notifications/channels")).json()["devices"]) == 1
    assert (await alice.get("/api/notifications/channels")).json()["devices"] == []
    # The same browser signs in as Alice and subscribes: the device moves to her.
    assert (await alice.post("/api/push/subscribe", json=sub)).status_code == 200
    assert (await client.get("/api/notifications/channels")).json()["devices"] == []
    assert len((await alice.get("/api/notifications/channels")).json()["devices"]) == 1
    # Alice cannot remove a device that is the owner's.
    sub2 = {**sub, "endpoint": "https://push.example/def"}
    await client.post("/api/push/subscribe", json=sub2)
    await alice.post("/api/push/unsubscribe", json={"endpoint": sub2["endpoint"]})
    assert len((await client.get("/api/notifications/channels")).json()["devices"]) == 1
    # The key pair is the server's, so every account subscribes with the same public key.
    assert (
        (await client.get("/api/notifications/channels")).json()["vapid_public_key"]
        == (await alice.get("/api/notifications/channels")).json()["vapid_public_key"]
    )


async def test_briefing_and_suggestions_are_per_user(client, alice, mock_llm):
    await client.post("/api/reminders", json={"text": "Dentist", "at": "2026-03-10T15:00"})
    mock_llm.script = ["Good morning, owner."]
    await client.post("/api/briefing/run")
    assert (await client.get("/api/briefing")).json()["latest"]["text"] == "Good morning, owner."
    assert (await alice.get("/api/briefing")).json() == {"latest": None, "history": []}
    assert (await alice.get("/api/suggestions")).json()["pending"] == 0


# -- plugins --------------------------------------------------------------------------


async def test_plugin_state_secrets_and_data_are_per_user(client, alice, app, plugin_dir):
    write_plugin(
        plugin_dir, "keeper",
        '''
        class P(Plugin):
            @tool("whoami", untrusted=False)
            async def whoami(self):
                return {"token": self.ctx.secret("token"), "note": await self.ctx.kv_get("note")}
            @tool("remember", untrusted=False)
            async def remember(self, note: str):
                await self.ctx.kv_set("note", note)
        ''',
        secrets=["token"],
    )
    for who in (client, alice):
        await who.post("/api/plugins/rescan")
    await client.put("/api/plugins/keeper/config", json={"secrets": {"token": "owner-token"}})
    await client.post("/api/plugins/keeper/enable")

    def state(r, name="keeper"):
        return next(p for p in r.json()["plugins"] if p["name"] == name)

    mine = state(await client.get("/api/plugins"))
    hers = state(await alice.get("/api/plugins"))
    assert mine["enabled"] and mine["status"] == "running" and mine["secrets"] == {"token": True}
    assert not hers["enabled"] and hers["secrets"] == {"token": False}

    c = app.state.c
    assert {"keeper_whoami", "keeper_remember"} <= {s["function"]["name"] for s in c.of(1).tools.specs()}
    assert not {s["function"]["name"] for s in c.of(2).tools.specs()} & {"keeper_whoami"}

    await alice.put("/api/plugins/keeper/config", json={"secrets": {"token": "alice-token"}})
    await alice.post("/api/plugins/keeper/enable")
    ok, out = await c.of(1).tools.call("keeper_remember", {"note": "owner note"}, _ctx())
    assert ok
    ok, out = await c.of(2).tools.call("keeper_whoami", {}, _ctx())
    assert ok and "alice-token" in out and "owner" not in out
    ok, out = await c.of(1).tools.call("keeper_whoami", {}, _ctx())
    assert "owner-token" in out and "owner note" in out


async def test_plugin_webhooks_reach_the_plugin_of_the_user_who_owns_the_token(
    client, alice, app, plugin_dir
):
    write_plugin(
        plugin_dir, "hooked",
        '''
        class P(Plugin):
            async def handle_webhook(self, headers, body):
                await self.ctx.kv_set("last", body.decode())
        ''',
        webhook=True,
    )
    tokens = {}
    for name, who in (("owner", client), ("alice", alice)):
        await who.post("/api/plugins/rescan")
        await who.post("/api/plugins/hooked/enable")
        plugin = next(p for p in (await who.get("/api/plugins")).json()["plugins"] if p["name"] == "hooked")
        tokens[name] = plugin["webhook_url"].split("token=")[1]
    assert tokens["owner"] != tokens["alice"]

    anonymous = browser(app)
    r = await anonymous.post(f"/api/plugins/hooked/webhook?token={tokens['alice']}", content=b"for alice")
    assert r.status_code == 200
    c = app.state.c
    assert await c.of(2).plugins.records["hooked"].instance.ctx.kv_get("last") == "for alice"
    assert await c.of(1).plugins.records["hooked"].instance.ctx.kv_get("last") is None


async def test_oauth_state_is_bound_to_the_user_who_started_it(client, alice, app):
    c = app.state.c
    url = c.of(1).plugins.oauth_begin("gmail", "https://assistant.test/api/oauth/callback")
    state = url.split("state=")[1].split("&")[0]
    assert c.oauth.pending_user(state) == 1
    from assistant.plugins.sdk import PluginError

    with pytest.raises(PluginError):
        await c.of(2).plugins.oauth_finish(state, "code")  # Alice cannot complete the owner's flow


def _ctx():
    from assistant.agent.tools import ToolContext

    return ToolContext(conversation_id=0)


# -- settings and roles ---------------------------------------------------------------


async def test_personal_settings_are_separate(client, alice):
    r = await client.patch("/api/settings", json={"values": {"briefing.time": "06:15", "timezone": "Europe/Paris"}})
    assert r.status_code == 200
    mine = (await client.get("/api/settings")).json()
    hers = (await alice.get("/api/settings")).json()
    assert mine["briefing"]["time"] == "06:15" and mine["timezone"] == "Europe/Paris"
    assert hers["briefing"]["time"] == "07:30" and hers["timezone"] == "UTC"
    assert hers["overridden"] == []

    r = await alice.patch("/api/settings", json={"values": {"notifications.quiet_hours.enabled": True}})
    assert r.status_code == 200
    assert (await client.get("/api/settings")).json()["notifications"]["quiet_hours"]["enabled"] is False


async def test_only_admins_change_server_wide_settings(client, alice):
    r = await alice.patch("/api/settings", json={"values": {"llm.chat.model": "other-model"}})
    assert r.status_code == 403
    r = await alice.patch("/api/settings", json={"values": {"agent.name": "Jarvis"}})
    assert r.status_code == 403
    assert (await alice.patch(
        "/api/settings", json={"values": {"notifications.telegram.bot_token": "123:abc"}})).status_code == 403
    assert (await alice.post("/api/settings/llm/test", json={"slot": "chat"})).status_code == 403
    hers = (await alice.get("/api/settings")).json()
    assert "llm" not in hers and hers["admin"] is False
    assert "bot_token" not in hers["notifications"]["telegram"]

    r = await client.patch("/api/settings", json={"values": {"agent.name": "Jarvis"}})
    assert r.status_code == 200
    assert (await alice.get("/api/settings")).json()["agent"] == {"name": "Jarvis"}
    mine = (await client.get("/api/settings")).json()
    assert mine["admin"] is True and mine["llm"]["chat"]["api_key"].startswith("…")


async def test_a_member_cannot_use_the_admin_api_or_mcp_controls(alice):
    assert (await alice.get("/api/users")).status_code == 403
    assert (await alice.post("/api/users", json={"username": "x"})).status_code == 403
    assert (await alice.request("DELETE", "/api/users/1", json={"password": PASSWORD})).status_code == 403
    assert (await alice.post("/api/mcp/anything/reconnect")).status_code == 403
    assert (await alice.get("/api/mcp")).status_code == 200  # the tool list is not secret


async def test_unauthenticated_requests_are_refused_everywhere(app):
    anon = browser(app)
    for path in ("/api/users", "/api/settings", "/api/memory/facts", "/api/reminders", "/api/audit"):
        assert (await anon.get(path)).status_code == 401, path


async def test_a_telegram_chat_links_to_one_account(client, alice, app, telegram, mock_llm):
    # The chat id in config.yaml is the owner's; other accounts start unlinked.
    assert (await client.get("/api/settings")).json()["notifications"]["telegram"]["chat_id"] == "42"
    assert (await alice.get("/api/settings")).json()["notifications"]["telegram"]["chat_id"] in (None, "")
    r = await alice.patch("/api/settings", json={"values": {"notifications.telegram.chat_id": "42"}})
    assert r.status_code == 409  # already the owner's
    r = await alice.patch("/api/settings", json={"values": {
        "notifications.telegram.chat_id": "99", "notifications.telegram.enabled": True}})
    assert r.status_code == 200

    from .test_notifications import _message

    c = app.state.c
    mock_llm.script = ["hi owner", "hi alice"]
    await c.telegram.handle_update(_message("hello", chat_id=42))
    await c.telegram.handle_update(_message("hello", chat_id=99, update_id=2))
    assert [p["chat_id"] for _, p in telegram] == ["42", "99"]
    assert [p["text"] for _, p in telegram] == ["hi owner", "hi alice"]
    # Each chat is a conversation of its own account.
    assert len((await client.get("/api/conversations")).json()) == 1
    assert len((await alice.get("/api/conversations")).json()) == 1
    # Strangers get no reply, and their messages reach nobody.
    await c.telegram.handle_update(_message("let me in", chat_id=555, update_id=3))
    assert len(telegram) == 2
    # A chat linked to two accounts (for example, by editing the database) is ignored.
    c.of(2).set_overrides({**c.of(2).overrides, "notifications.telegram.chat_id": "42"})
    await c.telegram.handle_update(_message("who am I", chat_id=42, update_id=4))
    assert len(telegram) == 2


async def test_file_defaults_for_who_gets_notified_apply_to_the_owner_only(app, alice, client):
    # config.yaml's Telegram chat and mail address name the owner; a new account must not
    # inherit them, or its notifications would go to the owner.
    app.state.c.config._base.setdefault("notifications", {}).setdefault("email", {})["to"] = "owner@test"
    app.state.c.config.version += 1
    c = app.state.c
    assert c.of(1).cfg().notifications.email.to == "owner@test"
    assert c.of(2).cfg().notifications.email.to is None
    assert c.of(2).cfg().notifications.telegram.chat_id is None
    # Everything else in the file (the bot token, privacy defaults, ...) is shared.
    assert c.of(2).cfg().notifications.telegram.bot_token == c.of(1).cfg().notifications.telegram.bot_token


async def test_notification_channels_are_each_users_own(client, alice, app, telegram, outbox):
    await client.patch("/api/settings", json={"values": {
        "notifications.email.enabled": True, "notifications.email.to": "owner@test",
        "notifications.default_channels": ["email"]}})
    r = await client.post("/api/notifications/test", json={"channel": "email"})
    assert r.json() == {"ok": True}
    assert [m["to"] for m in outbox] == ["owner@test"]
    # Alice has not turned email on: her channel is not ready, and nothing is sent for her.
    r = await alice.post("/api/notifications/test", json={"channel": "email"})
    assert r.json()["ok"] is False
    assert len(outbox) == 1


# -- administration -------------------------------------------------------------------


async def test_listing_and_updating_accounts(client, app):
    await add_user(app, "alice")
    users = (await client.get("/api/users")).json()
    assert [(u["username"], u["role"], u["owner"]) for u in users] == [
        ("owner", "admin", True), ("alice", "member", False)]
    assert "password_hash" not in str(users)

    r = await client.patch("/api/users/2", json={"display_name": "Alice A"})
    assert r.status_code == 200 and r.json()["display_name"] == "Alice A"
    # Promoting needs the admin's own password.
    assert (await client.patch("/api/users/2", json={"role": "admin"})).status_code == 403
    r = await client.patch("/api/users/2", json={"role": "admin", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert (await sign_in(app, "alice")).headers is not None
    # Both admins can be demoted only while another remains; the owner stays an admin here.
    r = await client.patch("/api/users/2", json={"role": "member", "password": PASSWORD})
    assert r.status_code == 200
    assert (await client.patch("/api/users/1", json={"role": "member", "password": PASSWORD})).status_code == 409


async def test_there_is_always_an_active_admin(client, app):
    await add_user(app, "alice", role="admin")
    alice = await sign_in(app, "alice")
    # Nobody can demote, disable or delete themselves.
    for body in ({"role": "member", "password": PASSWORD}, {"disabled": True, "password": PASSWORD}):
        assert (await alice.patch("/api/users/2", json=body)).status_code == 409
    assert (await alice.request("DELETE", "/api/users/2", json={"password": PASSWORD})).status_code == 409
    # The owner account cannot be deleted at all, and the last admin cannot be disabled.
    assert (await alice.request("DELETE", "/api/users/1", json={"password": PASSWORD})).status_code == 409
    assert (await alice.patch("/api/users/1", json={"disabled": True, "password": PASSWORD})).status_code == 200
    r = await client.get("/api/conversations")
    assert r.status_code == 401  # the owner was signed out when disabled
    assert (await alice.patch("/api/users/2", json={"role": "member", "password": PASSWORD})).status_code == 409


async def test_a_disabled_account_is_signed_out_and_stays_out(client, app, alice):
    r = await client.patch("/api/users/2", json={"disabled": True, "password": PASSWORD})
    assert r.status_code == 200 and r.json()["disabled"] is True
    assert (await alice.get("/api/conversations")).status_code == 401
    anyone = browser(app)
    r = await anyone.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 401 and r.json()["detail"] == "Wrong username or password"
    # Its scheduled work stops while it is disabled ...
    assert 2 not in [t.user_id for t in app.state.c.tenants.active()]
    # ... and resumes when it is enabled again.
    await client.patch("/api/users/2", json={"disabled": False})
    assert (await sign_in(app, "alice")).headers is not None
    assert 2 in [t.user_id for t in app.state.c.tenants.active()]


async def test_deleting_an_account_removes_everything_it_owned(client, app, alice, mock_llm, tmp_path):
    mock_llm.script = ["noted"]
    await alice.post("/api/chat", json={"text": "remember my wifi password is hunter2"})
    await alice.post("/api/memory/facts", json={"content": "Alice likes tea"})
    await alice.post("/api/reminders", json={"text": "tea", "at": "2026-03-10T15:00"})
    assert (await alice.post("/api/attachments", content=PNG)).status_code == 201
    await app.state.c.of(2).items.upsert("gmail", [Item(external_id="a", type="message", title="t", body="b")])
    await app.state.c.of(2).agent.drain()
    stored = [p for p in (tmp_path / "attachments").iterdir()]
    assert len(stored) == 1
    owner_before = await count(app, Conversation, user_id=1)

    assert (await client.request("DELETE", "/api/users/2", json={"password": "wrong"})).status_code == 403
    assert (await client.request("DELETE", "/api/users/2", json={"password": PASSWORD})).status_code == 200

    assert [u["username"] for u in (await client.get("/api/users")).json()] == ["owner"]
    for model in (Conversation, MemoryFact, Reminder, Attachment, Embedding, AuditEntry):
        assert await count(app, model, user_id=2) == 0, model.__name__
    async with app.state.c.db.session() as s:
        assert await s.get(User, 2) is None
        leftovers = (await s.scalars(select(Setting.key).where(Setting.key.startswith("user:2:")))).all()
    assert leftovers == []
    assert not list((tmp_path / "attachments").iterdir())
    assert app.state.c.tenants.find(2) is None
    assert (await alice.get("/api/conversations")).status_code == 401
    assert (await browser(app).post(
        "/api/auth/login", json={"username": "alice", "password": PASSWORD})).status_code == 401
    # The owner's data is untouched.
    assert await count(app, Conversation, user_id=1) == owner_before


async def test_admin_resets_are_recorded_in_both_logs(client, app, alice):
    r = await client.post("/api/users/2/reset-password", json={"password": "wrong"})
    assert r.status_code == 403
    r = await client.post("/api/users/2/reset-password", json={"password": PASSWORD})
    temp = r.json()["temporary_password"]
    assert (await alice.get("/api/conversations")).status_code == 401  # signed out everywhere
    fresh = browser(app)
    r = await fresh.post("/api/auth/login", json={"username": "alice", "password": temp})
    assert r.json()["user"]["must_change_password"] is True
    owner_log = [e["summary"] for e in (await client.get("/api/audit")).json()]
    assert any("Reset the password of alice" in s for s in owner_log)
    await fresh.post("/api/auth/password", json={"current": temp, "new": "something else entirely"})
    hers = [e["summary"] for e in (await fresh.get("/api/audit")).json()]
    assert any("Your password was reset by owner" in s for s in hers)

    r = await client.post("/api/users/2/reset-two-step", json={"password": PASSWORD})
    assert r.status_code == 200


async def test_an_admin_cannot_read_another_users_data(client, alice, mock_llm):
    """Administrators manage accounts; they get no view of what is in them."""
    mock_llm.script = ["ok"]
    r = await alice.post("/api/chat", json={"text": "private thought"})
    conv_id = r.json()["conversation_id"]
    await alice.post("/api/memory/facts", json={"content": "private fact"})
    assert (await client.get(f"/api/conversations/{conv_id}/messages")).status_code == 404
    assert "private" not in (await client.get("/api/memory/facts")).text
    users = (await client.get("/api/users")).text
    assert "private" not in users


# -- the schema -----------------------------------------------------------------------


async def test_every_per_user_table_has_a_user_id(app):
    from sqlalchemy import inspect

    from assistant.core.models import Base

    shared = {"users", "settings", "messages", "proactive_marks"}  # messages hang off conversations
    async with app.state.c.db.engine.connect() as conn:
        tables = await conn.run_sync(lambda sync: {t: [c["name"] for c in inspect(sync).get_columns(t)]
                                                   for t in inspect(sync).get_table_names()})
    for table in Base.metadata.tables:
        if table not in shared:
            assert "user_id" in tables[table], f"{table} is not scoped to a user"


def test_nothing_imports_the_old_single_user_container_fields():
    """The handlers must get a user's services through ``mine(request)``, never from the
    shared container, where there would be no way to say whose they are."""
    import pathlib
    import re

    pattern = re.compile(
        r"\bc\.(reminders|items|notifier|memory|webpush|briefing|proactive|audit|actions|media|agent)\b"
    )
    offenders = []
    for path in pathlib.Path("assistant").rglob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path}:{number}: {line.strip()}")
    assert offenders == []

