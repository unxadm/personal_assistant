# Personal Assistant

A self-hosted assistant for one person or a few (a household, a small team) that works with any OpenAI-compatible LLM endpoint. Every person has their own private assistant on the same server.

**Status: all six phases built.** See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the
design and [docs/SECURITY.md](docs/SECURITY.md) for what is protected and what is not.

## What works today

- Chat in a web UI (installable as a PWA) behind a password, with optional two-step sign-in
  (authenticator app and recovery codes).
- Several people on one server ([Several users](#several-users)): each has their own
  conversations, memory, reminders, connected Google accounts, notifications and settings,
  and nobody (admins included) can read anyone else's.
- Any OpenAI-compatible provider, with retries, exponential backoff and a fallback model.
- Tool calling, with an automatic JSON-in-prompt fallback for models that lack it.
- Long-term memory: the assistant saves facts about you; you can view, edit and delete them.
- Semantic search (embeddings + sqlite-vec) over facts and past conversations, pulled into
  context before each answer.
- Rolling summaries for long conversations.
- Settings page for all model slots with a "Test connection" button; API keys saved there are
  encrypted at rest.
- Plugins, turned on and off at runtime from the Plugins page:
  - **Gmail**: sync, search, read, flag bills/receipts/travel/packages/deadlines, write
    drafts (the assistant never sends mail in a conversation). Pub/Sub push with polling
    fallback. Optionally sends the assistant's own notification emails to you.
  - **Google Calendar**: sync, list, find free time and conflicts, create/update/delete
    events. Push channels with polling fallback.
  - Examples: a local to-do list (`plugins/tasks`) and weather (`plugins/weather`).
- MCP servers: connect any [MCP](https://modelcontextprotocol.io) server (a local program or
  an HTTP endpoint) and the assistant can use its tools. Their results count as outside
  content, and each call waits for your approval unless you mark the tool read-only.
- Read-only by default: write access to Gmail and Calendar is a separate opt-in setting.
- Approval gate: an action that changes something outside the app is held for your approval
  whenever email, event or other third-party content was involved in the request.
- Privacy exclusions: senders, labels and calendars that are never stored or shown to the model.
- Reminders in natural language ("remind me every Monday at 9 to send the report"), one-off
  or repeating, with snooze and done. They survive restarts; ones missed while the server was
  down are delivered when it comes back.
- Notifications by browser/phone push, Telegram and email, with a channel choice per kind of
  notification, quiet hours, and Done / Snooze buttons on push and Telegram.
- Chat with the assistant from Telegram.
- A detailed daily briefing on its own page: today's schedule, tasks and reminders, mail that
  needs attention, what is coming tomorrow, and what to do first.
- Voice: press Speak in chat, or send a Telegram voice note; it is transcribed by your
  speech-to-text model. Replies can be played back with a Listen button.
- Images: attach, paste or photograph a receipt, flyer, whiteboard or screenshot (in chat or
  Telegram). The assistant reads it and suggests what to do, such as a reminder or an event.
- Proactive suggestions: bills, deadlines, appointments and requests found in email, calendar
  conflicts, meetings with no travel time, early starts, meeting briefs, package updates and
  mail that is waiting for your reply. Each one says why it is shown and waits for you.
- Activity log: everything the assistant did on your behalf, with undo where it can be reversed.

## Setup

Requires Python 3.12+ and Node 20+ (Node only to build the UI).

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

```bash
(cd web && npm install && npm run build)
```

```bash
cp config.example.yaml config.yaml && cp .env.example .env
```

Edit `config.yaml` (model, timezone) and put your key in `.env`, then:

```bash
set -a; . ./.env; set +a; .venv/bin/python -m assistant
```

Open <http://127.0.0.1:8000>. The first visit asks you to create the first account (a
username and password); it is the admin. Database migrations run automatically on start.

### With Docker

```bash
cp config.example.yaml config.yaml && cp .env.example .env && mkdir -p data
```

Edit `config.yaml` and `.env`, then:

```bash
docker compose up -d --build
```

The first visit needs a one-time setup code as well as the new account, because the
container listens on a network address:

```bash
docker compose logs app | grep "setup code"
```

The compose file publishes the port on `127.0.0.1` only, runs as your user with a read-only
filesystem, and keeps everything in `./data`. If your user id is not 1000, set `PA_UID` and
`PA_GID` in `.env`. For local models, `docker compose --profile ollama up -d` adds Ollama;
point `llm.chat.base_url` at `http://ollama:11434/v1`.

### Before exposing it beyond your machine

Put a reverse proxy with HTTPS in front, set `server.public_url`, and turn on two-step
sign-in under Settings → Sign-in security. The full checklist is in
[docs/SECURITY.md](docs/SECURITY.md).

For UI development run `npm run dev` in `web/` (proxies `/api` to port 8000).

## Choosing an LLM provider

Set these in `config.yaml`, via `PA_…` environment variables, or in the Settings page.

| Provider | `llm.chat.base_url` | Example `model` | Key |
|---|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` | `PA_LLM__CHAT__API_KEY` |
| OpenRouter | `https://openrouter.ai/api/v1` | `anthropic/claude-sonnet-4.5` | OpenRouter key |
| Ollama (local) | `http://localhost:11434/v1` | `llama3.1` | none |

Notes:

- **Embeddings** are a separate slot (`llm.embeddings`). OpenRouter users typically point it
  at another server; Ollama users can use `nomic-embed-text`. Without an embeddings model the
  assistant still works, minus semantic search.
- **Models without tool calling**: leave `tool_mode: auto`. The first rejected request
  switches that model to JSON-in-prompt tool calling; set `tool_mode: json` to force it.
- `temperature` / `max_tokens` are only sent when set, because some models reject them.
- Changing the embeddings model leaves old vectors unsearchable. Plugin data can be
  re-indexed with "Delete synced data" followed by "Sync now"; there is no re-index for
  facts and past conversations yet.

## Voice and images

All three are model slots on the Settings page (or `llm.stt`, `llm.tts`, `llm.vision` in
`config.yaml`), each with its own optional `base_url` and key.

- **Images** work out of the box if your chat model can see images. If it cannot, set a
  **Vision** model: it describes each image and the chat model works from the description.
- **Speech-to-text** turns on the Speak button and Telegram voice notes. Any server with an
  OpenAI-style `/v1/audio/transcriptions` endpoint works: OpenAI (`whisper-1`), or Whisper
  on your own machine, for example [Speaches](https://github.com/speaches-ai/speaches)
  (formerly faster-whisper-server) or the `whisper.cpp` server. Point the slot's base URL at
  it, e.g. `http://localhost:8000/v1`. With a local server, recordings never leave your
  machine.
- **Text-to-speech** is optional and adds a Listen button to replies. It uses
  `/v1/audio/speech` (OpenAI `tts-1`, or a local server such as Speaches with Kokoro/Piper).

Notes:

- The browser only allows microphone access over HTTPS or on `localhost`.
- Recordings are sent to the speech-to-text model and not stored. Images are stored under
  `<data_dir>/attachments` and deleted with their conversation.
- Photos are resized in the browser (longest side 1600 px) before upload.
- Anything in a picture is treated like email content: if the assistant wants to change
  something outside the app because of it, you are asked to approve first.

## Google Cloud setup (Gmail and Calendar)

You create your own OAuth client, so tokens are issued to you and nobody else.

1. Open <https://console.cloud.google.com/>, create a project (any name).
2. **APIs & Services → Library**: enable **Gmail API** and **Google Calendar API**.
3. **APIs & Services → OAuth consent screen** (also shown as *Google Auth Platform*):
   - User type **External**, fill in the app name and your email.
   - Under **Audience**, add your own Google address as a **test user**.
   - While the app is in *Testing*, Google expires refresh tokens after 7 days and you will
     have to reconnect weekly. To avoid that, press **Publish app** (*In production*). You do
     not need to complete verification for personal use; you will see an "unverified app"
     warning once when connecting — choose *Advanced → Go to …*.
4. **Credentials → Create credentials → OAuth client ID**:
   - Application type **Web application**.
   - **Authorized redirect URI**: the address you open the assistant at, plus
     `/api/oauth/callback` — for example `http://127.0.0.1:8000/api/oauth/callback`.
     It must match exactly (`localhost` and `127.0.0.1` are different). Google accepts plain
     `http` only for localhost addresses; anything else must be `https`. If you set
     `server.public_url`, use that as the base instead.
5. Put the client ID and secret in `.env`, then restart:

   ```
   PA_OAUTH__GOOGLE__CLIENT_ID=1234-abc.apps.googleusercontent.com
   PA_OAUTH__GOOGLE__CLIENT_SECRET=GOCSPX-...
   ```

6. In the web UI open **Plugins**, turn on **gmail** and/or **google_calendar**, and press
   **Connect Google account**. Each plugin asks only for its own scopes:

   | Plugin | Always | Only if you enable writing |
   |---|---|---|
   | gmail | `gmail.readonly` | `gmail.compose` ("Allow creating drafts"), `gmail.send` ("Allow sending notification email") |
   | google_calendar | `calendar.events.readonly` | `calendar.events` ("Allow creating, changing and deleting events") |

   Google has no drafts-only scope; `gmail.compose` technically also permits sending, but the
   plugin only ever calls `drafts.create`. After switching a write option on, press
   **Reconnect** to grant the extra scope.

The first Gmail sync imports the last 14 days (configurable); after that it is incremental.

### Push instead of polling (optional)

Without push, Gmail is polled every 5 minutes and Calendar every 10. Push needs the server
to be reachable from the internet over HTTPS; set `server.public_url`.

- **Gmail**: in Google Cloud create a Pub/Sub **topic**, grant
  `gmail-api-push@system.gserviceaccount.com` the *Pub/Sub Publisher* role on it, and add a
  **push subscription** whose endpoint is the *Webhook URL* shown under the Gmail plugin's
  details (it contains a secret token). Then enter the topic name
  (`projects/<project>/topics/<topic>`) in the Gmail plugin settings.
- **Calendar**: nothing to configure. With an HTTPS `public_url` the plugin opens and renews
  watch channels itself. Google may require the domain to be verified in Search Console.

## Notifications

Everything here is under **Settings → Notifications**; `config.yaml` / `.env` work too (see
[config.example.yaml](config.example.yaml)). With no channel set up, reminders still appear
on the Reminders page when they go off.

### Browser and phone push

Open the assistant in the browser you want notified, go to Settings → Notifications and
press **Turn on for this device**. Repeat on each device.

- Push only works over HTTPS, or on `localhost`. To get it on a phone, serve the assistant
  over HTTPS (reverse proxy, Tailscale, …) and set `server.cookie_secure: true`.
- iPhone/iPad: first add the app to the Home Screen (Share → Add to Home Screen), open it
  from there, then turn notifications on.
- Messages are encrypted for your browser; Google's, Mozilla's or Apple's push service only
  relays them.

### Android app

There is a native Android client in [android/](android/README.md): chat with pictures and
dictation, the suggestion inbox, reminders, the briefing and the activity log. It gets
notifications by asking your server every 15 minutes (every minute while open) instead of
going through a push service, so they can arrive a little late.

### Telegram

One bot serves the whole server; each person links their own chat to it.

1. An admin: in Telegram, message **@BotFather**, send `/newbot` and follow the prompts. It
   replies with a token like `123456789:AA…`. Paste it under Settings → Notifications →
   Telegram → Bot token (only admins see this field) and save.
2. Everyone: tick **Use Telegram** under Settings → Notifications → Telegram, save, then open
   the bot in Telegram and send `/start`. It replies with your chat ID.
3. Enter that chat ID in Settings and save. Press **Send test**.

From then on your reminders arrive there with Done / Snooze buttons, and you can chat with
your assistant in that chat (`/new` starts a fresh conversation). A chat ID can be linked to
one account only, and only linked chats are served; anyone else who finds the bot gets no
reply. The server polls Telegram, so it does not need to be reachable from the internet.
Telegram can read what the bot sends and receives.

### Email

Email is sent through the Gmail API from the Gmail account you connected, so there is no
mail server or password to configure:

1. On the **Plugins** page, open Gmail, turn on **Allow sending notification email**, save,
   and reconnect the account (each person connects their own). Google asks for one more permission, `gmail.send`, which can
   send mail but not read, change or delete it.
2. Under **Settings → Notifications → Email**, turn email on. Mail goes to that Gmail
   address unless you enter another one under "Send to".

Only the notification channel uses this. The assistant has no tool for sending mail in a
conversation; it can still only save drafts, and only if you allowed that.

### Quiet hours and the daily briefing

- **Quiet hours** hold notifications and deliver them when the period ends. A reminder set
  with urgency "high" ("…and wake me if you have to") comes through anyway.
- **Daily briefing** is written once a day at the time you choose, by the chat model from
  that day's data, and has its own **Briefing** page (with the last two weeks below it and
  "Write a new one" for one on demand). By default it is detailed: a few hundred words
  under the headings Schedule, Tasks and reminders, Mail, Looking ahead and Suggested
  focus, including overlaps, tomorrow, tasks due in the next few days and the weather if
  the weather plugin is on. Set `briefing.detail: short` (Settings → Notifications → Length)
  for the 150-word version. Notifications carry the opening of a long briefing and a link
  to the page.

## Proactive suggestions

The assistant looks at new mail and at your calendar in the background and files what it
notices under **Inbox**. Nothing there takes effect until you press its button.

How it decides:

1. **Fixed rules run on everything**, at no cost: the Gmail plugin's keyword signals, and
   calendar checks (overlaps, two places with too little time in between, a start before
   08:00 tomorrow, a meeting in the next 30 minutes with other attendees, a request that has
   had no reply for two days).
2. **Only mail the rules flag** as a bill, deadline, request or travel booking is sent to
   your chat model, which answers in a fixed format. It is given no tools, and anything
   outside that format is thrown away. Mail older than three days is not read, and at most
   30 emails an hour are.

Each suggestion shows a confidence, a "Why am I seeing this?" explanation, and **Useful** /
**Not useful** buttons. Feedback raises or lowers how readily that kind of suggestion is
shown; two "not useful" for one sender and their mail stops being read by the model. The
Inbox shows what feedback has changed.

Under **Settings → Proactive suggestions** each kind has a level:

| Level | What happens |
|---|---|
| Off | Never shown |
| Tell me only | Shown, with no action offered |
| Suggest and wait | Shown with a button: set the reminder, add the event, draft the reply |
| Do it automatically | The reminder is created straight away and you are told, with an Undo |

"Do it automatically" exists only for kinds whose action is a reminder, and needs a
confidence of 0.8 or more. Calendar events and drafts always wait for you, because their
content comes from email written by someone else.

Notifications about suggestions are capped (3 an hour, 10 a day by default) and respect
quiet hours; anything over the cap waits in the Inbox without a ping.

## Activity log

**Activity** lists what the assistant did for you: every tool it ran in chat, actions it held
for approval and what you decided, reminders it set from suggestions, and what it did on its
own. Created reminders, remembered facts and created calendar events have an **Undo**.
Things that cannot be taken back (a deleted event, a cancelled reminder) are listed without
one.

## Plugins

Third-party plugins live in `./plugins` (configurable with `plugins.dirs`). Folders starting
with `_` are ignored. To write one, copy `plugins/_template` and follow
[docs/PLUGINS.md](docs/PLUGINS.md). Plugins are Python code running inside the app: install
only ones you trust.

## MCP servers

List servers under `mcp.servers` in `config.yaml` (see [config.example.yaml](config.example.yaml)),
restart, and their tools are offered to the model as `mcp_<server>_<tool>`. The Plugins page
shows each server's status and tools, with a Reconnect button.

- **Only in the file.** A server's `command` is a program run on this machine, so servers
  cannot be added or changed from the web UI or the Settings API. The program gets a minimal
  environment (`PATH`, `HOME`, ...) plus the `env` you list; the app's own secrets are not
  passed on. In Docker, the command has to exist inside the container.
- **Approval by default.** The app cannot tell what an arbitrary tool does, so every call
  waits for your approval in the app (Inbox → pending actions) unless you list the tool under
  `read_only_tools` (never asks), or `auto_approve_tools` (runs at once, like the calendar
  tools, but is held whenever mail, web pages or another tool's result are involved).
  `trust_read_only_hints: true` makes the server's own `readOnlyHint` count as `read_only_tools`;
  a server can claim anything, so it is off.
- **Results are untrusted.** Whatever a server returns is treated as outside content: it is
  marked as data, and from then on changes made by other tools wait for approval too.
- **Fixed tool list.** Tools are listed once per connection. A server that changes its tools
  later is not picked up until it reconnects. It is not offered sampling or questions to you.
- Everything it does shows in the Activity log. A server that cannot start or that drops is
  retried with a growing delay, and left off after five failures until you press Reconnect.

## Configuration

Precedence: defaults < `config.yaml` < environment (`PA_` prefix, `__` between levels) <
Settings page. The Settings page can change two kinds of setting:

- **Server-wide** (admins only): `llm.*`, `agent.*`, and the Telegram bot token and API base
  and the web push contact under `notifications.*`.
- **Personal** (every user, for themselves only): `privacy.*`, `briefing.*`, `proactive.*`,
  `timezone` and the rest of `notifications.*`. Values in `config.yaml` are the defaults
  everyone starts from; what someone saves overrides them for that person only. The two
  values that say *who* is notified, `notifications.telegram.chat_id` and
  `notifications.email.to`, apply to the owner (the first account) only, so a new account
  never inherits someone else's chat or address.

Server, auth, OAuth client, database and MCP server settings are file/env only. See [config.example.yaml](config.example.yaml).

## Several users

The first account created is the **owner** and an admin. Admins open the **People** tab to
add accounts, disable them, reset passwords or two-step sign-in, make someone an admin, or
delete an account and everything it owns. A new account gets a generated password (shown
once) that its owner must change at the first sign-in.

What is separate for every person: conversations, long-term memory and search, reminders,
the suggestion inbox and briefings, notifications and their channels (push devices, the
Telegram chat, the Gmail address), plugins with their settings and OAuth accounts, uploads,
the activity log, and the personal settings above. What is shared: the server itself, the
model endpoint and its key (so everyone's messages go to the same provider), MCP servers,
installed plugin code, and the Telegram bot.

Admins manage accounts and server settings; they cannot read other people's data through
the app. They can, however, reset someone's password, so tell people that an admin of
your server is trusted with access to the machine and the database file in any case. See
[docs/SECURITY.md](docs/SECURITY.md). There is no self-registration. The Android app has a
username field (empty is fine while there is one account); first-time password changes
happen in the web app.

Upgrading from a single-user install needs nothing: the migration turns the existing data
into the first account (username `owner`) with the password and two-step sign-in you
already had.

## Tests

```bash
.venv/bin/pytest
```

The suite runs against an in-process mock OpenAI-compatible server
([tests/conftest.py](tests/conftest.py)); no network or API key is needed. It covers the
plugin loader, reminder parsing and scheduling, the LLM adapter, an end-to-end reminder
flow, the proactive engine, sign-in security ([tests/test_security.py](tests/test_security.py)),
prompt injection ([tests/test_injection.py](tests/test_injection.py)), user isolation and
account administration ([tests/test_multiuser.py](tests/test_multiuser.py)) and the
upgrade from a single-user database ([tests/test_migration.py](tests/test_migration.py)).

The Android client has its own tests: `cd android && ./gradlew testDebugUnitTest lintDebug`.

## Layout

```
assistant/
  core/     config, database + models, encryption, settings store, user accounts
  llm/      OpenAI-compatible adapter, JSON tool-calling fallback
  memory/   long-term facts, embedding store + semantic search
  agent/    conversation pipeline, tool registry, built-in tools
  mcpclient/ MCP servers as tool sources
  plugins/  SDK, loader/manager, OAuth, item store, built-in gmail + google_calendar
  media/          upload type detection, image storage
  reminders/      reminder storage and state, RRULE handling, LLM tools
  notifications/  dispatcher (routing, quiet hours, retries), web push, Telegram, email
  scheduler/      tick loop, daily briefing
  proactive/      rules, model screening of mail, suggestion inbox, feedback
  audit/          audit log and undo
  api/      FastAPI app, auth, REST routes, per-user service stacks (tenants.py)
  migrations/  Alembic
plugins/    third-party plugins: _template, tasks, weather
web/        React + Vite + Tailwind UI
android/    native Android client (Kotlin + Compose)
tests/
docs/       ARCHITECTURE.md, PLUGINS.md, SECURITY.md
Dockerfile, docker-compose.yml
```
