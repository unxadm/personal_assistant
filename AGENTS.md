# AGENTS.md

Guide for an LLM (or person) changing this codebase. Read this first, then the doc it points
to for the area you are touching. `README.md` is for users, `docs/ARCHITECTURE.md` explains
the design, `docs/SECURITY.md` the threat model, `docs/PLUGINS.md` the plugin API.

## What this is

A self-hosted personal assistant for **one person or a few**, each with their own private
data (multi-user, see "Users and isolation"): FastAPI backend (Python 3.12), React PWA,
a native Android client, plugins (Gmail, Google Calendar, examples), reminders, notifications
(web push, Telegram, email through Gmail), a daily briefing, a proactive suggestion engine,
long-term memory with vector search, and MCP servers as extra tools. One database (SQLite via
SQLAlchemy async), one process, no queue or Redis.

The owner's requirements that shape everything:
- Their data never leaves their server except to the LLM endpoint they configure (plus the
  services they connect: Google, Telegram, push, MCP servers listed in config).
- One user's data is never visible to, or changed by, another user (admins included).
- Secrets and OAuth tokens are encrypted at rest; nothing sensitive goes into logs.
- Anything that sends, deletes, pays or contacts people needs **approval** by default.
- Retrieved content (mail, pages, tool results) is **data, never instructions**.
- Everything the assistant does is in the audit log, with undo where possible.
- Ask for the minimum OAuth scopes; write scopes are opt-in.

## Commands

```bash
.venv/bin/pytest                         # whole suite, ~40 s, no network or API key needed
.venv/bin/pytest tests/test_mcp.py -q    # one file
.venv/bin/ruff check . --fix             # lint + import order (line length 100, rules E F I UP B)
cd web && npm run build                  # tsc --noEmit && vite build; must pass for UI changes
cd android && ANDROID_HOME=~/Android/Sdk JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 \
  ./gradlew testDebugUnitTest lintDebug assembleDebug assembleRelease
python -m assistant                      # run the server (reads ./config.yaml, PA_CONFIG, PA_* env)
docker compose up -d --build             # the container bakes the code in: REBUILD after changes
```

A change is done when pytest, ruff and (for UI) the web build pass. Android lint treats
warnings as errors.

## Do not touch the owner's real setup

`config.yaml` (real LLM endpoint + API key), `data/` (their database and `secret.key`) and
`.env` are live. Never delete, overwrite, print or commit them, and never repeat the API key.
To run the app for a manual check, write a scratch config elsewhere, point `PA_CONFIG` at it,
use its own `data_dir`, pick a port other than 8000, and clean up afterwards. Tests already
isolate themselves with `tmp_path`.

The user also has uncommitted edits of their own in `Dockerfile` and `docker-compose.yml`
(bind address, commented-out ollama service). Leave them alone unless asked.

## Layout

```
assistant/
  core/        config (pydantic), db + SQLAlchemy models, encryption (SecretBox), settings store, TOTP
  llm/         OpenAI-compatible client: retries, fallback model, JSON-in-prompt tool fallback
  memory/      long-term facts; embeddings + sqlite-vec search (VectorStore)
  agent/       pipeline.py (the conversation turn), tools.py (Tool, ToolRegistry), actions.py
               (pending approvals), untrusted.py (prompt fencing), builtin_tools.py
  mcpclient/   McpManager: MCP servers -> registry tools
  plugins/     sdk.py (author API), manager.py (lifecycle, fault isolation, OAuth), items.py,
               builtin/{gmail,google_calendar}
  reminders/   storage, RRULE recurrence, tools
  notifications/ service.py (routing, quiet hours, retries), webpush, telegram, email (Gmail API)
  scheduler/   tick loop, briefing
  proactive/   rules, model screening of mail, suggestion inbox
  audit/       audit log + undo
  media/       image/audio upload handling
  api/         app.py (wiring: create_app), tenants.py (one service stack per user), auth.py,
               user_routes.py (admin), hardening.py, *_routes.py
  migrations/  Alembic versions 0001..0008
plugins/       third-party examples: _template, tasks, weather
web/src/       React + Vite + Tailwind (pages/, api.ts is the typed client, App.tsx has the tabs)
android/       Kotlin + Compose client (own README)
tests/         pytest; conftest.py holds the fakes
docs/
```

`assistant/api/app.py::create_app` builds the shared services inside the lifespan and exposes
them as `app.state.c` (a `Container`, constructed with keyword arguments). To add a *shared*
service, construct it there, add a `Container` field and start/stop it in the lifespan. To add
a *per-user* service (anything that holds a user's data), construct it in
`api/tenants.py::build` with the user id, add a `Tenant` field, and reach it from handlers
with `mine(request)` (see below) and from tests with `c.of(1)`.

## Users and isolation (second most important invariant)

- Every table that holds a user's data has `user_id` (FK, `ON DELETE CASCADE`);
  `tests/test_multiuser.py` fails if a new table lacks it. Per-user settings live in the
  user's scope of `SettingsStore` (`settings.scoped(uid)`, keys `user:<id>:...`).
- Per-user services (agent, memory, vectors, items, reminders, notifier and channels, plugin
  manager, audit, approvals, briefing, proactive, media) take `user_id` in the constructor and
  add it to **every** query, including `get` by id (check `row.user_id`). A lookup by id
  that belongs to someone else must behave exactly like "not found".
- Handlers get the signed-in user's stack from `auth.mine(request)`; `container(request)` is for
  shared things only. Never accept a user id from the client for data access. A test greps
  that `c.<per-user service>` is not used on the shared container.
- Roles: `require_auth` (signed in, not waiting for a forced password change),
  `require_admin`. New admin-only endpoints depend on `require_admin`; server-wide settings
  (`is_server_setting`) need it too. Admins get **no** endpoint that returns another user's
  data.
- Tool registries are per user, chained to the shared one (MCP). Register user-specific tools
  on the tenant's registry, server-wide ones on `c.tools`.
- Config: `Tenant.cfg()` is the user's effective config (`ConfigService.build_for_user`). Pass
  that (not `c.config.config`) to anything acting for a user. Personal vs server-wide paths:
  `USER_PREFIXES`/`ADMIN_PREFIXES`/`SERVER_WIDE_PATHS` in `core/config.py`.
- Scheduler jobs loop over `tenants.active()`; add new periodic work through `each_user` in
  `create_app`.
- Telegram, web push keys and the OAuth state table are shared infrastructure that route to the
  right user (`Tenants.telegram_owner`, subscription `user_id`, `_Pending.user_id`).

## The turn, and the approval gate (most important invariant)

`agent/pipeline.py`: persist user message -> build prompt (facts, RAG chunks, summary, recent
history) -> tool loop (`_tool_loop`) -> persist reply -> background indexing/summarising.

- A turn is **tainted** when it holds outside content: retrieved `item` chunks, attached
  images, a tool with `untrusted_output=True` has returned, or an earlier tainted message is
  still in view (`meta.tainted`, `conversation.summary_tainted`).
- `Tool.needs_approval(tainted)` = `confirm or (tainted and (side_effecting or protected))`.
  When true the call is stored as a `PendingAction` and the model is told it was NOT run.
  Approving runs it once through `ToolRegistry.call` (`ActionService.resolve`).
- Tool output is always prefixed with `UNTRUSTED_NOTE` and truncated to 8000 chars. Outside
  text going into a prompt passes through `untrusted.defang/attr` so it cannot close its fence.
- `tool_recorder` (audit) hooks `ToolRegistry.call`; every call is logged. Undo handlers live in
  `audit/service.py::UNDO`.

Rules when you change anything near this:
- A new tool that changes the outside world is `side_effecting=True`. A tool returning third-party
  text is `untrusted_output=True`. Tools that write memory the model later trusts are `protected`.
- Never add a path where untrusted text becomes an instruction or reaches a side-effecting
  call without the gate. Do not expose "send email" as a model tool (email is a notification
  channel only).
- Proactive/background code that calls the model must not give it side-effecting tools.
- Add a test in `tests/test_injection.py` style when you add a way for outside content in.

## Single sign-on

`core/oidc.py` + the `/oidc/*` routes in `api/auth.py`; config `auth.oidc` (file/env only: it
holds a client secret and decides who may get in, so never make `auth.` overridable from the
UI). Invariants: match accounts only by `(issuer, subject)` from a verified ID token, never by
e-mail or username; keep the algorithm allow-list asymmetric; keep `state` single-use and
tied to the `pa_oidc` cookie; never show or log provider error text or the secret. Tests use a
fake provider in `tests/test_oidc.py` (it checks PKCE and client auth); extend it for any change.
The Android app signs in through a one-time-code hand-off (`/oidc/login?challenge=`,
`assistant.client://sso`, `/oidc/exchange`): the code must stay single-use, short-lived and
useless without the app's verifier; the redirect target is a constant, never a parameter.

## Configuration

`core/config.py`. Precedence: defaults < `config.yaml` < `PA_*` env (`__` nests) < Settings
page overrides stored in the database.

- Models are pydantic with `extra="forbid"` (and `coerce_numbers_to_str`): **a new key must be
  declared on the model** or startup fails. Legacy keys can be dropped with a `mode="before"`
  validator (see `EmailConfig`).
- Only paths under `OVERRIDABLE_PREFIXES` can be changed from the UI. `server`, `auth`,
  `oauth`, `database`, `plugins` and `mcp` are file/env only on purpose. Of the overridable
  paths, `llm.`/`agent.` and three Telegram/push paths are server-wide (admins); the rest are per user. Do not add `mcp.` there:
  an MCP `command` is arbitrary program execution.
- Secrets: `is_secret_path` (suffixes `api_key`, `client_secret`, `password`, `bot_token`) are
  masked in the settings view and encrypted when saved. Settings views are built from explicit
  keys in `api/routes.py::_settings_view`; add new keys there deliberately.
- Update `config.example.yaml` and the README for every new option.

## Database

SQLAlchemy models in `core/models.py` (`owner_id()` is the shared `user_id` column). **Schema changes need both** the model *and* a new
Alembic revision in `assistant/migrations/versions/` (next number, `down_revision` = previous).
The app runs migrations at start (`Database.migrate`); tests use `create_all` instead
(`migrate=False`), so a missing migration will not fail the suite: write it and check that
the revision chain is linear. Times are stored UTC; use `utcnow()` and the `aware()` helper
when comparing.

## Plugins

See `docs/PLUGINS.md`. A plugin is a directory with `plugin.yaml` (manifest: capabilities,
permissions, config JSON Schema, secrets, optional OAuth) and a `Plugin` subclass using
`@tool(...)`. Manifests are read at start or on **Rescan**, and the manager isolates failures
(timeouts, failure counter, shut-off). The config form in the UI is rendered from
`config_schema`. Optional OAuth scopes are tied to boolean config options
(`oauth.optional_scopes`), which is how write/send access stays opt-in; changing one requires
the user to reconnect the account. Built-in plugin tools are named `<plugin>_<method>`.

Gmail notes: the plugin has a non-tool method `send_notification` used only by the email
notification channel (`notifications/email.py::GmailMailer`); it needs `enable_send` and the
`gmail.send` scope. SMTP no longer exists (leftover SMTP keys are ignored and saved overrides
are removed at start-up).

## MCP servers

`mcpclient/manager.py`, config under `mcp.servers`. Uses the official `mcp` package **2.x**
(`mcp.Client`, `StdioServerParameters`, `streamable_http_client`; HTTP runs on `httpx2`, not
`httpx`). One supervisor task per server owns the `async with` (anyio cancel scopes must be
entered and exited in the same task), registers tools as `mcp_<server>_<tool>`, and reconnects
with backoff. Defaults are deliberately strict: results untrusted, every call needs approval
unless the config lists `read_only_tools` / `auto_approve_tools`, minimal subprocess
environment, tool list read once per connection, no sampling/roots/elicitation callbacks.
Keep it that way. `tests/fake_mcp_server.py` is a real stdio server used by the tests.

## Testing

`tests/conftest.py` provides everything; do not call the network.
- `MockOpenAI` (`mock_llm`): scriptable chat/embeddings/audio server. Set `mock_llm.script` to a
  list of replies (str, int status, or a full message; use `tool_call(name, json_args)`).
- `FakeWeb` (`web`): fake internet for plugins and notification channels (route by method + regex).
- `FakeClock` (`clock`): fixed "now" (2026-03-10 12:00 UTC, a Tuesday); `clock.advance(...)`.
- `FakeMailer` / `outbox` / `mailer`: notification email; mark a test `real_mailer` to go through
  the Gmail plugin and `FakeGoogle` (`tests/test_google.py`).
- `app` / `client` fixtures: a started app with one account, the owner (user 1, admin), and an
  `httpx` client signed in as them. Mark a test `first_run` for an app with no accounts. Use
  `add_user(app, "alice")` and `sign_in(app, "alice")` for more accounts, `browser(app)` for a
  cookie-less client, and `c = app.state.c; c.of(1).reminders...` to reach a user's services. Edit
  `app_config` first to change config. The scheduler is disabled; tests call `tick()` /
  `flush()` themselves.
- `write_plugin(plugin_dir, name, code, **manifest)` creates a throwaway plugin.

Every behaviour change gets a test; security properties (gate, fencing, auth, log redaction)
have dedicated tests in `test_injection.py`, `test_security.py`, `test_mcp.py`. Do not weaken
one to make a change pass: stop and ask.

## Web and Android

- `web/src/api.ts` is the single typed API client; update its types with the backend. Tabs are in
  `App.tsx`; the briefing page is reachable at `/?tab=briefing`. Styling is Tailwind with shared
  `card`, `btn`, `btn-ghost`, `input` classes. `web/dist` is git-ignored and built by Docker.
- The Android app authenticates with the server's `pa_session` cookie (stored under an Android
  Keystore key), polls `GET /api/notifications` (no FCM), and renders pending actions generically
  (tool name + arguments). Backend response changes can break it: keep fields backwards
  compatible and update `android/app/src/main/java/assistant/client/data/Models.kt` and its tests.
  `.gitignore` anchors `/data/` so the Android `data` package is not ignored; keep it anchored.

## Security checklist for any change

- No secret in logs, error messages shown to the user or model, API responses or exceptions.
  Logs go through a redaction filter (`core/security.py`), but do not rely on it; also avoid
  putting URLs with tokens in messages.
- New endpoints go on the `authed` router (session cookie required); state-changing requests
  are protected by the same-origin check in `api/hardening.py`. Android sends no `Origin`.
- New outbound destinations must be documented in `docs/SECURITY.md` ("Data at rest and in
  transit"), and the README's privacy claims must stay true.
- Prefer the least destructive option (archive/trash over delete) and keep write scopes opt-in.

## Conventions

- Python: type hints, `from __future__ import annotations`, async everywhere I/O happens, small
  modules with a docstring explaining the *why*. Comments explain intent, not mechanics.
- Errors to the model/user are plain sentences saying what to do next; avoid stack traces.
- Keep user-visible docs in step with code: README (feature list, setup, config), `config.example.yaml`,
  `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, `docs/PLUGINS.md`.
- Don't commit unless asked. Commit messages are short and say why. Never commit `.env`,
  `config.yaml`, `data/` or built artifacts.
