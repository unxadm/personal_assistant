# Writing a plugin

A plugin is a folder with two files. Gmail and Google Calendar are built exactly this way
(see `assistant/plugins/builtin/`), and `plugins/tasks` and `plugins/weather` are small
working examples.

## Quick start (about ten minutes)

1. Copy the template:

   ```bash
   cp -r plugins/_template plugins/hello
   ```

2. In `plugins/hello/plugin.yaml` set `name: hello`. Remove what you don't need: for a
   tools-only plugin keep `capabilities: [tools]` and delete `sync_interval`, `secrets`
   and `permissions.network`.

3. In `plugins/hello/plugin.py` keep the `greet` tool and delete `sync`.

4. Open **Plugins** in the web UI, press **Rescan**, then **Turn on**. No restart. Ask the
   assistant "say hello to Sam" and it will call `hello_greet`.

After editing code, press **Turn off** / **Turn on** (or **Rescan** if you changed the
manifest) to reload it.

## The manifest: `plugin.yaml`

| Field | Meaning |
|---|---|
| `name` | Lowercase id (`[a-z][a-z0-9_]*`). Tools are registered as `<name>_<method>`. |
| `version`, `description` | Shown in the plugin manager. |
| `entrypoint` | `<module>:<Class>`, module relative to the folder. Default `plugin:Plugin`. |
| `capabilities` | Any of `data_source`, `tools`, `triggers`, `notification_channel`. |
| `sync_interval` | Seconds between `sync()` calls (min 30). Omit for on-demand only. |
| `webhook` | `true` to receive POSTs at `/api/plugins/<name>/webhook`. |
| `permissions.network` | Hosts `ctx.http` may call. `*.example.com` wildcards allowed. |
| `permissions.data_write` | Item types the plugin may write. |
| `permissions.data_read` | `true` to read other plugins' items. |
| `permissions.notify` | `true` to send the user notifications with `ctx.notify`. |
| `secrets` | Names of secrets the user enters in the UI. Stored encrypted. |
| `config_schema` | JSON Schema (`type: object`) for settings; rendered as a form. |
| `oauth` | OAuth connection, see below. |

Anything not declared is refused at runtime with `PermissionDenied`. Unknown manifest
fields are rejected, so typos are caught when the plugin is scanned.

Settings form: `string`, `integer`, `number`, `boolean`, `enum` and arrays of strings are
supported. `title`, `description` and `default` are used; defaults are applied before your
code sees the config.

## The class

```python
from assistant.plugins.sdk import Item, Plugin, PluginError, tool

class MyPlugin(Plugin):
    async def setup(self): ...            # enabled, or settings changed
    async def teardown(self): ...         # disabled or app shutdown
    async def sync(self): ...             # data sources
    async def handle_webhook(self, headers, body): ...
```

Everything is `async`. Each call runs under a timeout (`plugins.call_timeout`, or
`plugins.sync_timeout` for `sync`) and an exception guard:

- An exception is recorded and shown in the plugin manager; the app keeps running.
- After `plugins.max_failures` consecutive failures the plugin is shut off until the user
  turns it on again.
- Raise `PluginError("message")` for expected problems ("feature is turned off", "no such
  task"). In a tool the message goes to the model and does not count as a failure.
- Raise `NeedsAuth` when credentials are rejected; the UI then asks the user to reconnect.

### Tools

```python
@tool(
    "Describe when the model should use this.",
    {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    side_effecting=False,
    untrusted=True,
)
async def lookup(self, city: str):
    return {"anything": "JSON-serializable"}
```

- `side_effecting=True` for anything that sends, deletes, pays, or contacts other people.
  Such a call runs directly when the user asks for it in a turn with no external content;
  if external content is in play it is held until the user approves it.
- `untrusted=True` (the default) means the result contains text written by third parties.
  It marks the turn as containing external content. Set `untrusted=False` only when the
  output is purely the user's own data or computed by you.

### Data sources

Normalize what you fetch into `Item`s and hand them over:

```python
await self.ctx.upsert_items([
    Item(
        external_id="stable-id-from-the-source",   # same id next sync = update, not duplicate
        type="message",   # item | event | task | message | contact | document | transaction | location
        title="Subject or name",
        body="Main text (this is what gets embedded for search)",
        url="https://link.back/to/source",
        occurred_at=..., starts_at=..., ends_at=..., due_at=...,   # whichever apply
        sender="Name <a@b.c>",                # used by the "exclude senders" privacy rule
        participants=["a@b.c"], labels=["Inbox"],
        data={"anything": "type-specific"},   # data["calendar"] is used by "exclude calendars"
    )
])
await self.ctx.delete_items(["id-that-no-longer-exists"])
```

The host skips unchanged items, embeds new and changed ones for semantic search, applies
the user's privacy exclusions, and publishes `item.created` / `item.updated` /
`item.deleted` events. Keep cursors with `ctx.kv_get` / `ctx.kv_set`.

If a tool returns **live** data that did not go through `upsert_items`, filter it with
`ctx.is_excluded(labels=..., sender=..., calendar=...)` so excluded data never reaches the
model.

## The context: `self.ctx`

| Member | Purpose |
|---|---|
| `config` | Validated settings (dict). |
| `secret(name)` | A declared secret, or `None`. |
| `timezone` | The user's `ZoneInfo`. |
| `await http(method, url, *, oauth=False, **httpx_kwargs)` | HTTP to a declared host. No redirects are followed. `oauth=True` adds and refreshes the bearer token. |
| `await upsert_items(items)` / `delete_items(ids)` / `query_items(...)` | Common data model. |
| `is_excluded(...)` | Check the user's privacy rules. |
| `await emit(event_type, payload)` | Publish `<plugin>.<event_type>` for the proactive engine (needs `triggers`). |
| `await notify(title, body="", *, url=None, urgency="normal")` | Notify the user through their channels (needs `permissions.notify`). Quiet hours apply unless urgency is `"high"`. |
| `await kv_get(key, default)` / `kv_set(key, value)` | Small persistent JSON state. |
| `webhook_url`, `webhook_base_url`, `webhook_token` | Inbound webhook address and its secret. |
| `request_sync()` | Schedule a sync soon (typical webhook handler body). |
| `log` | A logger named `plugin.<name>`. |

## Notification channels

A plugin with the `notification_channel` capability can deliver notifications (to Slack,
ntfy, a pager, …). Implement `send`:

```python
async def send(self, notification):   # assistant.plugins.sdk.Notification
    await self.ctx.http("POST", "https://ntfy.example/mytopic", content=notification.title,
                        headers={"Priority": "5" if notification.urgency == "high" else "3"})
```

`notification` has `title`, `body`, `url`, `urgency` (`low`/`normal`/`high`), `category`
(`reminder`, `briefing`, `plugin`, `system`), `ref` and `actions`. While the plugin is
running it appears next to the built-in channels under Settings → Notifications, where the
user chooses which kinds of notification it receives. Raise an exception if delivery fails;
the host then tries the user's other channels.

## Webhooks

With `webhook: true`, POSTs to `/api/plugins/<name>/webhook` are passed to
`handle_webhook(headers, body)` if they carry the plugin's token, either as `?token=...`
or in an `X-Goog-Channel-Token` header. The full URL is shown under the plugin's details.
Return quickly; call `self.ctx.request_sync()` and do the work in `sync()`.

## OAuth

```yaml
oauth:
  provider: google                     # or give auth_url and token_url for another provider
  scopes: [https://www.googleapis.com/auth/gmail.readonly]
  optional_scopes:                     # requested only when this boolean setting is on
    enable_drafts: [https://www.googleapis.com/auth/gmail.compose]
```

Ask for the least you need and put write scopes under `optional_scopes`. The host runs the
authorization-code flow with PKCE, stores tokens encrypted, refreshes them, and moves the
plugin to "Needs sign-in" when the grant is revoked or a newly enabled option needs more
scope. For `provider: google` the client id and secret come from `oauth.google` in the app
config; for other providers declare `client_id` and `client_secret` under `secrets`.

## Testing a plugin

`tests/conftest.py` has helpers used by the project's own tests: `write_plugin` creates a
plugin in a temp folder, `FakeWeb` answers the plugin's outbound HTTP, and the `app` /
`client` fixtures run the whole server. `tests/test_plugins.py` shows the pattern.

## Isolation: what it is and is not

Plugins run inside the app process. The guard protects the app from plugin bugs — crashes,
hangs in `await`ed code, and repeated failures. Permissions are enforced on everything
that goes through `ctx`. This is not a sandbox against hostile code: a plugin is Python
and could bypass `ctx` (open its own sockets, read files) or block the event loop with
CPU-bound code. Install only plugins you trust, as you would any Python package.
