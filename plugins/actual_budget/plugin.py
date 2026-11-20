"""Actual Budget (https://actualbudget.org) through its official Node library.

@actual-app/api is a JavaScript package that downloads a local copy of the budget, so this
plugin runs ``bridge.mjs`` with Node, once per operation, and talks to it in JSON. The
Python side owns everything that matters for safety:

* credentials go to the helper on stdin, never in arguments or the environment;
* the helper gets a minimal environment and a private temporary directory that is deleted
  afterwards, so no copy of the budget is left on disk;
* the Node program and the helper script are fixed (a user cannot point the plugin at some
  other program), and the server address is checked;
* what the helper prints is parsed, never executed, and its error text is never passed on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import shutil
import signal
import tempfile
import time
from datetime import date, datetime, timedelta
from datetime import time as time_of_day
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from assistant.plugins.sdk import Item, Plugin, PluginError, tool

HERE = Path(__file__).resolve().parent
BRIDGE = HERE / "bridge.mjs"
# Server-wide settings (environment, not plugin settings: users must not be able to change them).
ENV_NODE = "ACTUAL_BUDGET_NODE"
ENV_HOSTS = "ACTUAL_BUDGET_ALLOWED_HOSTS"
# Passed through to Node when set, so a private certificate authority keeps working.
PASS_THROUGH_ENV = ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE")

TOOL_TIMEOUT = 25.0  # the host stops a tool call at plugins.call_timeout (30 s by default)
SYNC_TIMEOUT = 240.0
MAX_OUTPUT = 20 * 1024 * 1024
MARK = "RESULT "
SOURCE_KIND = "budget"
# Actual's server allows 5 failed sign-ins per 15 minutes (per address) and then refuses even
# the right password, so a wrong password must not be retried on every tool call and sync.
RATE_PAUSE = 15 * 60.0


def find_node() -> str:
    node = os.environ.get(ENV_NODE) or shutil.which("node")
    if not node or not os.access(node, os.X_OK):
        raise PluginError(
            "Node.js 22 or newer is needed for this plugin and was not found "
            f"(install it, or set {ENV_NODE} to its path)."
        )
    return node


def check_server_url(raw: str) -> str:
    """The address, normalised. Only http(s) to a named host, optionally from an allow-list."""
    url = (raw or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PluginError("The server address must start with http:// or https://.")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise PluginError("Leave credentials, query strings and fragments out of the server address.")
    allowed = [h.strip().lower() for h in os.environ.get(ENV_HOSTS, "").split(",") if h.strip()]
    if allowed and parts.hostname.lower() not in allowed:
        raise PluginError("This server only allows connections to the hosts its administrator listed.")
    return url


def cents(amount: object) -> int:
    """A positive amount in currency units as whole cents (no float rounding surprises)."""
    try:
        value = Decimal(str(amount))
    except InvalidOperation:
        raise PluginError("The amount is not a number.") from None
    if not value.is_finite() or value <= 0 or value >= 10**9:
        raise PluginError("The amount must be a positive number below one billion.")
    return int((value * 100).quantize(Decimal(1)))


class ActualBudgetPlugin(Plugin):
    # What the server last refused, so it is not asked again until the settings change.
    _refused: tuple[str, str] | None = None
    _rate_until: float = 0.0

    # -- the helper process -----------------------------------------------------------

    @property
    def symbol(self) -> str:
        return str(self.ctx.config.get("currency_symbol", "$"))[:4]

    def money(self, amount: int) -> str:
        sign = "-" if amount < 0 else ""
        return f"{sign}{self.symbol}{abs(amount) / 100:,.2f}"

    async def setup(self) -> None:
        # Called again whenever the settings or secrets change: a new password gets a new try.
        self._refused, self._rate_until = None, 0.0
        check_server_url(self.ctx.config["server_url"])
        find_node()
        if not self.ctx.config.get("budget_id", "").strip():
            raise PluginError("Enter the budget's sync id.")

    async def _run(self, op: str, args: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        cfg = self.ctx.config
        node = find_node()
        password = self.ctx.secret("password")
        if not password:
            raise PluginError("Enter the Actual Budget server password.")
        credentials = (cfg["server_url"], password)
        if self._refused == credentials:
            raise PluginError(
                "Actual Budget refused the password. Correct it in this plugin's settings "
                "(it is not tried again until then, so the server does not lock you out)."
            )
        if time.monotonic() < self._rate_until:
            raise PluginError(
                "Actual Budget is refusing sign-ins for now (too many failed attempts). "
                "Try again in about 15 minutes."
            )
        with tempfile.TemporaryDirectory(prefix="actual-budget-") as work:
            request = {
                "op": op, "args": args, "dataDir": work,
                "serverURL": check_server_url(cfg["server_url"]),
                "syncId": cfg["budget_id"].strip(),
                "password": password,
                "filePassword": self.ctx.secret("file_password") or "",
            }
            env = {
                "PATH": str(Path(node).parent), "HOME": work, "TMPDIR": work, "NODE_ENV": "production",
                **{k: os.environ[k] for k in PASS_THROUGH_ENV if k in os.environ},
            }
            proc = await asyncio.create_subprocess_exec(
                node, str(BRIDGE), cwd=HERE, env=env, start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=MAX_OUTPUT,
            )
            try:
                line = await asyncio.wait_for(self._converse(proc, json.dumps(request).encode()), timeout)
            except TimeoutError:
                raise PluginError("Actual Budget took too long to answer.") from None
            finally:
                # The answer is in (or the call failed or was cancelled): nothing the helper
                # still has running is wanted, whether or not it closed its pipes.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
            code = proc.returncode
        return self._answer(line, code, credentials)

    @staticmethod
    async def _converse(proc: asyncio.subprocess.Process, payload: bytes) -> bytes | None:
        """Send the request, then read until the helper's marked answer line (not until it
        closes its output: the library may leave things running)."""
        assert proc.stdin and proc.stdout
        try:
            proc.stdin.write(payload)
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            return None
        try:
            while line := await proc.stdout.readline():
                if line.startswith(MARK.encode()):
                    return line
        except (ValueError, asyncio.LimitOverrunError):  # an answer beyond the size limit
            return None
        return None

    def _answer(self, line: bytes | None, code: int | None, credentials: tuple[str, str]) -> dict[str, Any]:
        reply = None
        if line is not None:
            with contextlib.suppress(ValueError):
                reply = json.loads(line.decode(errors="replace")[len(MARK):])
        if not isinstance(reply, dict):
            self.ctx.log.warning("the Actual Budget helper gave no usable answer (exit %s)", code)
            raise PluginError("The Actual Budget helper failed to run. Is Node installed and `npm install` done?")
        if not reply.get("ok"):
            error = reply.get("error") or {}
            # The helper's messages are fixed sentences of its own, not the server's text.
            message = str(error.get("message") or "Actual Budget returned an error.")[:300]
            self.ctx.log.warning("Actual Budget: %s", error.get("code", "error"))
            if error.get("code") == "auth":
                self._refused = credentials
            elif error.get("code") == "rate":
                self._rate_until = time.monotonic() + RATE_PAUSE
            raise PluginError(message)
        data = reply.get("data")
        return data if isinstance(data, dict) else {}

    # -- sync -------------------------------------------------------------------------

    def today(self) -> date:
        return datetime.now(self.ctx.timezone).date()

    async def sync(self) -> None:
        cfg = self.ctx.config
        today = self.today()
        this_month = today.strftime("%Y-%m")
        previous = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        args: dict[str, Any] = {"months": [this_month, previous]}
        days = int(cfg.get("sync_transactions_days", 60))
        if days > 0:
            args |= {"start": (today - timedelta(days=days)).isoformat(), "end": today.isoformat()}
        data = await self._run("snapshot", args, timeout=SYNC_TIMEOUT)

        items = [self._accounts_item(data.get("accounts", []))]
        items += [self._month_item(m) for m in data.get("months", [])]
        transactions = [self._transaction_item(t) for t in data.get("transactions", [])]
        await self.ctx.upsert_items(items + transactions)

        # Transactions that left the window (or were deleted in Actual) leave the search too.
        current = {i.external_id for i in transactions}
        known = set(await self.ctx.kv_get("transactions", []))
        stale = sorted(known - current)
        if stale:
            await self.ctx.delete_items(stale)
        await self.ctx.kv_set("transactions", sorted(current))

    def _accounts_item(self, accounts: list[dict[str, Any]]) -> Item:
        shown = [a for a in accounts if not self.ctx.is_excluded(labels=[a["name"]])]
        lines = [
            f"{a['name']}: {self.money(a['balance'])}"
            + (" (closed)" if a["closed"] else "") + (" (off budget)" if a["off_budget"] else "")
            for a in shown if not a["closed"]
        ]
        return Item(
            external_id="accounts", type="item", title="Actual Budget: account balances",
            body="\n".join(lines), occurred_at=datetime.now(self.ctx.timezone),
            data={"kind": SOURCE_KIND, "what": "accounts"},
        )

    def _month_item(self, m: dict[str, Any]) -> Item:
        lines = [
            f"To budget: {self.money(m['to_budget'])}. Budgeted {self.money(m['total_budgeted'])}, "
            f"income {self.money(m['total_income'])}, spent {self.money(m['total_spent'])}."
        ]
        for group in m.get("groups", []):
            if group.get("hidden") or self.ctx.is_excluded(labels=[group["name"]]):
                continue
            for c in group.get("categories", []):
                if c.get("hidden") or self.ctx.is_excluded(labels=[c["name"]]):
                    continue
                if not (c["budgeted"] or c["spent"] or c["balance"]):
                    continue
                lines.append(
                    f"{group['name']} / {c['name']}: budgeted {self.money(c['budgeted'])}, "
                    f"spent {self.money(c['spent'])}, left {self.money(c['balance'])}"
                )
        return Item(
            external_id=f"month:{m['month']}", type="item", title=f"Actual Budget: budget for {m['month']}",
            body="\n".join(lines), data={"kind": SOURCE_KIND, "what": "month", "month": m["month"]},
        )

    def _transaction_item(self, t: dict[str, Any]) -> Item:
        when = datetime.combine(date.fromisoformat(t["date"]), time_of_day(12, 0), tzinfo=self.ctx.timezone)
        payee = t["payee"] or "Transaction"
        where = ", ".join(x for x in (t["account"], t["category"]) if x)
        body = f"{payee}: {self.money(t['amount'])} on {t['date']} ({where})"
        if t["notes"]:
            body += f". {t['notes']}"
        return Item(
            external_id=f"transaction:{t['id']}", type="transaction", title=payee, body=body,
            occurred_at=when,
            # The user's privacy rule "exclude labels" can name an account or a category.
            labels=[x for x in (t["account"], t["category"]) if x],
            data={
                "kind": SOURCE_KIND, "what": "transaction", "amount": t["amount"],
                "account": t["account"], "category": t["category"], "payee": t["payee"],
                "cleared": t["cleared"], "transfer": t["transfer"],
            },
        )

    # -- tools ------------------------------------------------------------------------

    def tools(self):
        wanted = list(super().tools())
        if not self.ctx.config.get("enable_writes"):
            wanted = [t for t in wanted if t.name != "add_transaction"]
        return wanted

    def _view(self, t: dict[str, Any]) -> dict[str, Any]:
        return {
            "date": t["date"], "payee": t["payee"], "amount": self.money(t["amount"]),
            "account": t["account"], "category": t["category"], "notes": t["notes"],
            "cleared": t["cleared"], "transfer": t["transfer"],
        }

    @tool(
        "The user's Actual Budget accounts with their current balances.",
        {"type": "object", "properties": {}},
        # Names and balances are the user's own data.
        untrusted=False,
    )
    async def accounts(self) -> Any:
        data = await self._run("accounts", {}, timeout=TOOL_TIMEOUT)
        return {
            "accounts": [
                {"name": a["name"], "balance": self.money(a["balance"]), "off_budget": a["off_budget"]}
                for a in data.get("accounts", [])
                if not a["closed"] and not self.ctx.is_excluded(labels=[a["name"]])
            ]
        }

    @tool(
        "The Actual Budget envelope budget for one month: how much is budgeted, spent and left "
        "in each category. Use for 'how much is left for groceries' or 'am I overspending'.",
        {
            "type": "object",
            "properties": {"month": {"type": "string", "description": "YYYY-MM; the current month if omitted."}},
        },
        untrusted=False,
    )
    async def budget_month(self, month: str = "") -> Any:
        month = month.strip() or self.today().strftime("%Y-%m")
        try:
            datetime.strptime(month, "%Y-%m")
        except ValueError:
            raise PluginError("The month must look like 2026-03.") from None
        m = await self._run("month", {"month": month}, timeout=TOOL_TIMEOUT)
        groups = []
        for g in m.get("groups", []):
            if g.get("hidden") or self.ctx.is_excluded(labels=[g["name"]]):
                continue
            cats = [
                {"category": c["name"], "budgeted": self.money(c["budgeted"]),
                 "spent": self.money(c["spent"]), "left": self.money(c["balance"])}
                for c in g.get("categories", [])
                if not c.get("hidden") and not self.ctx.is_excluded(labels=[c["name"]])
            ]
            groups.append({"group": g["name"], "categories": cats})
        return {
            "month": m.get("month", month),
            "to_budget": self.money(m.get("to_budget", 0)),
            "total_budgeted": self.money(m.get("total_budgeted", 0)),
            "total_income": self.money(m.get("total_income", 0)),
            "total_spent": self.money(m.get("total_spent", 0)),
            "groups": groups,
        }

    @tool(
        "Transactions from Actual Budget, newest first. Filter by account, dates (YYYY-MM-DD) "
        "or a word in the payee, category or notes. Negative amounts are spending. Payee names "
        "and notes come from banks and shops.",
        {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "Account name; all open accounts if omitted."},
                "start_date": {"type": "string", "description": "Default: 30 days ago."},
                "end_date": {"type": "string", "description": "Default: today."},
                "search": {"type": "string", "description": "Word to look for."},
                "limit": {"type": "integer", "description": "At most this many (default 30, max 200)."},
            },
        },
    )
    async def transactions(
        self, account: str = "", start_date: str = "", end_date: str = "", search: str = "", limit: int = 30
    ) -> Any:
        today = self.today()
        try:
            start = date.fromisoformat(start_date) if start_date else today - timedelta(days=30)
            end = date.fromisoformat(end_date) if end_date else today
        except ValueError:
            raise PluginError("Dates must look like 2026-03-31.") from None
        if end < start:
            raise PluginError("The end date is before the start date.")
        limit = max(1, min(int(limit), 200))
        needle = search.strip().lower()
        data = await self._run(
            "transactions",
            {"account": account.strip() or None, "start": start.isoformat(), "end": end.isoformat()},
            timeout=TOOL_TIMEOUT,
        )
        rows = [
            t for t in data.get("transactions", [])
            if not self.ctx.is_excluded(labels=[x for x in (t["account"], t["category"]) if x])
            and (not needle or needle in f"{t['payee']} {t['category']} {t['notes']}".lower())
        ]
        return {"count": min(len(rows), limit), "matching": len(rows), "transactions": [self._view(t) for t in rows[:limit]]}

    @tool(
        "Add one transaction to Actual Budget. Give the amount as a positive number and say "
        "whether it is an expense or income. Only when the user asked for it.",
        {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "Account name."},
                "amount": {"type": "number", "description": "Positive, in currency units (12.50)."},
                "kind": {"type": "string", "enum": ["expense", "income"], "description": "Default expense."},
                "payee": {"type": "string"},
                "category": {"type": "string", "description": "Category name, if the user named one."},
                "notes": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD; today if omitted."},
            },
            "required": ["account", "amount"],
        },
        side_effecting=True,
        untrusted=False,
    )
    async def add_transaction(
        self, account: str, amount: float, kind: str = "expense", payee: str = "",
        category: str = "", notes: str = "", date: str = "",  # noqa: A002
    ) -> Any:
        if not self.ctx.config.get("enable_writes"):
            raise PluginError("Adding transactions is turned off in this plugin's settings.")
        if kind not in ("expense", "income"):
            raise PluginError('The kind must be "expense" or "income".')
        if isinstance(amount, float) and not math.isfinite(amount):
            raise PluginError("The amount is not a number.")
        when = (date or "").strip() or self.today().isoformat()
        try:
            when = datetime.strptime(when, "%Y-%m-%d").date().isoformat()
        except ValueError:
            raise PluginError("The date must look like 2026-03-31.") from None
        value = cents(amount) * (-1 if kind == "expense" else 1)
        result = await self._run(
            "add_transaction",
            {
                "account": account.strip(), "amount": value, "date": when,
                "payee": payee.strip()[:200], "category": category.strip()[:200], "notes": notes.strip()[:500],
            },
            timeout=TOOL_TIMEOUT,
        )
        self.ctx.request_sync()
        return {"added": True, "account": result.get("account", account), "amount": self.money(value), "date": when}
