"""The real helper (Node + @actual-app/api) against a real Actual server. Skipped unless set:

    cd plugins/actual_budget && npm install
    ACTUAL_BUDGET_LIVE='http://localhost:5006|server-password|budget-sync-id' pytest tests/test_actual_budget_live.py

Use a throwaway budget. Two tests are opt-in because they have side effects:
ACTUAL_BUDGET_LIVE_WRITE=1 adds one transaction; ACTUAL_BUDGET_LIVE_BADPASSWORD=1 makes one failed
sign-in (Actual's server refuses everyone for 15 minutes after five of them)."""

from __future__ import annotations

import json
import os

import pytest

from .test_plugins import REPO, manager, rescan

LIVE = os.environ.get("ACTUAL_BUDGET_LIVE", "")
pytestmark = pytest.mark.skipif(LIVE.count("|") != 2, reason="ACTUAL_BUDGET_LIVE is not set")


async def enable(client, app, password=None, **config):
    url, secret, sync_id = (LIVE.split("|") + ["", "", ""])[:3]
    manager(app).app_config().plugins.dirs.append(str(REPO / "plugins"))
    await rescan(client)
    r = await client.put("/api/plugins/actual_budget/config", json={
        "config": {"server_url": url, "budget_id": sync_id, **config},
        "secrets": {"password": password or secret}})
    assert r.status_code == 200, r.text
    return (await client.post("/api/plugins/actual_budget/enable")).json()


def payload(out: str) -> dict:
    return json.loads(out[out.index("{"):])


async def test_reading_a_real_budget(client, app):
    assert (await enable(client, app))["status"] == "running"
    manager(app).app_config().plugins.call_timeout = 120
    ok, out = await app.state.c.of(1).tools.call("actual_budget_accounts", {}, None)
    assert ok and payload(out)["accounts"]
    ok, out = await app.state.c.of(1).tools.call("actual_budget_budget_month", {}, None)
    assert ok and payload(out)["groups"]
    ok, out = await app.state.c.of(1).tools.call("actual_budget_transactions", {"start_date": "2000-01-01"}, None)
    assert ok and "transactions" in payload(out)

    assert (await client.post("/api/plugins/actual_budget/sync")).json()["ok"]
    items = await app.state.c.of(1).items.query(plugin="actual_budget", limit=500)
    assert any(i["external_id"] == "accounts" for i in items)


@pytest.mark.skipif(not os.environ.get("ACTUAL_BUDGET_LIVE_BADPASSWORD"), reason="counts as a failed sign-in")
async def test_a_wrong_password_is_reported_plainly(client, app):
    await enable(client, app, password="definitely-wrong")
    manager(app).app_config().plugins.call_timeout = 120
    ok, out = await app.state.c.of(1).tools.call("actual_budget_accounts", {}, None)
    assert not ok and "refused the password" in out and "definitely-wrong" not in out


@pytest.mark.skipif(not os.environ.get("ACTUAL_BUDGET_LIVE_WRITE"), reason="writes to the budget")
async def test_adding_a_real_transaction(client, app):
    await enable(client, app, enable_writes=True)
    manager(app).app_config().plugins.call_timeout = 120
    _, out = await app.state.c.of(1).tools.call("actual_budget_accounts", {}, None)
    account = payload(out)["accounts"][0]["name"]
    ok, out = await app.state.c.of(1).tools.call(
        "actual_budget_add_transaction", {"account": account, "amount": 1.23, "payee": "Live test"}, None)
    assert ok and '"added": true' in out
    ok, out = await app.state.c.of(1).tools.call(
        "actual_budget_transactions", {"search": "Live test", "start_date": "2000-01-01"}, None)
    assert ok and payload(out)["matching"] >= 1
