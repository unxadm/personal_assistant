# Actual Budget plugin

Lets the assistant read your [Actual Budget](https://actualbudget.org) (accounts and
balances, this month's envelope budget, transactions) and, if you allow it, add transactions.
It uses the official [`@actual-app/api`](https://www.npmjs.com/package/@actual-app/api) npm
package, which is a JavaScript library: the plugin starts a small Node program
(`bridge.mjs`) for each operation and talks to it in JSON.

## Install

You need **Node.js 22 or newer** on the machine that runs the assistant, then:

```bash
cd plugins/actual_budget
npm install
```

Then in the web interface: **Plugins → Rescan → Actual Budget**, fill in

| Setting | Where to find it |
|---|---|
| Actual server address | The address you open Actual at, e.g. `https://actual.example.com` |
| Budget sync id | Actual → Settings → *Show advanced settings* → **Sync ID** |
| Server password (secret) | The password you sign in to Actual with |
| File password (optional secret) | Only if you turned on end-to-end encryption for the budget |

and **Turn on**. It syncs every six hours (the plugin's sync button does it at once).

Only password sign-in is supported. Actual's server refuses everyone for 15 minutes after
five failed sign-ins, so a wrong password is not retried until you change it.

### Docker

The shipped `Dockerfile` already includes Node 22 and this plugin's packages (built inside the
image, because the library has a native part), so you only need to rebuild:

```bash
docker compose up -d --build
```

Do **not** run `npm install` in this folder on the host when you use Docker:
`docker-compose.yml` mounts `./plugins` into the container, and packages built for your
machine's Node would be found before the image's and fail to load. If you did, delete
`plugins/actual_budget/node_modules`. Run `npm install` only for the non-Docker setup above.

## What it does

- **Sync** (data source): account balances, the budget for this and last month, and the last
  *N* days of transactions (default 60; 0 turns transactions off) are stored as items, so
  the assistant can find them by meaning ("what did I spend at the pharmacy?"). Transactions that leave the window or are deleted in Actual
  are removed again.
- **Tools:** `actual_budget_accounts`, `actual_budget_budget_month`,
  `actual_budget_transactions` (filter by account, dates, word), and
  `actual_budget_add_transaction`, which exists **only** when *Let the assistant add
  transactions* is turned on. It is held for your approval whenever the conversation
  contains outside content; amounts are exact (cents), expenses are negative.
- Payees and notes come from banks and shops, so transactions count as **outside content**
  (fenced, never instructions). Account and category names are treated as your own data.
- **Privacy:** the global *Keep out of AI processing → Email labels* list also matches
  account and category names ("Medical", "Savings"): matching transactions, categories and
  accounts are never stored or shown to the model.

## How it runs (and what that means)

Each operation downloads the budget into a private temporary directory, answers, signs out
and deletes the directory; no copy of your budget is kept on disk and nothing is cached
between calls (a call takes a few seconds). The password reaches the helper on stdin, not in
its arguments or environment, and the helper gets a minimal environment.

Unlike Python plugins that use `ctx.http`, this helper makes its own network connection,
so the manifest's `permissions.network` does not limit it. The Node program and the helper
script are fixed; the server address must be `http(s)://` to a named host. On a server with
several users, an administrator can restrict the hosts with the environment variable
`ACTUAL_BUDGET_ALLOWED_HOSTS` (comma-separated), and choose the Node binary with
`ACTUAL_BUDGET_NODE`. Both are deliberately not plugin settings.

## Tests

`tests/test_actual_budget.py` runs everything on the Python side against a fake `node`.
`tests/test_actual_budget_live.py` runs the real helper against a real server (see its
docstring); use a throwaway budget.
