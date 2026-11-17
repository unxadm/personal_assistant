import { useEffect, useState, type ReactNode } from "react";
import { api, type McpServer, type PluginInfo, type PluginList, type SchemaProperty } from "../api";

const STATUS: Record<PluginInfo["status"], { label: string; className: string }> = {
  running: { label: "Running", className: "bg-emerald-950 text-emerald-300" },
  disabled: { label: "Off", className: "bg-slate-800 text-slate-400" },
  needs_config: { label: "Needs settings", className: "bg-amber-950 text-amber-300" },
  needs_auth: { label: "Needs sign-in", className: "bg-amber-950 text-amber-300" },
  error: { label: "Stopped after errors", className: "bg-red-950 text-red-300" },
};

function oauthNotice(): { ok: boolean; text: string } | null {
  const params = new URLSearchParams(location.search);
  const outcome = params.get("oauth");
  if (!outcome) return null;
  history.replaceState(null, "", location.pathname);
  const plugin = params.get("plugin") || "the plugin";
  return outcome === "connected"
    ? { ok: true, text: `Account connected for ${plugin}.` }
    : { ok: false, text: `Connecting ${plugin} failed: ${params.get("message") ?? "unknown error"}` };
}

export default function Plugins() {
  const [data, setData] = useState<PluginList | null>(null);
  const [error, setError] = useState("");
  const [notice] = useState(oauthNotice);
  const [mcp, setMcp] = useState<McpServer[]>([]);

  const load = () =>
    api
      .plugins()
      .then(setData)
      .catch((e: Error) => setError(e.message));

  useEffect(() => {
    load();
    api.mcpServers().then((r) => setMcp(r.servers)).catch(() => {});
  }, []);

  const replace = (info: PluginInfo) =>
    setData((d) => d && { ...d, plugins: d.plugins.map((p) => (p.name === info.name ? info : p)) });

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl space-y-4 p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h1 className="text-lg font-semibold">Plugins</h1>
            <p className="text-sm text-slate-400">
              Data sources and tools. Changes apply immediately, without a restart.
            </p>
          </div>
          <button
            className="btn-ghost shrink-0"
            title="Look for added, changed or removed plugins in the plugin folders"
            onClick={() => api.rescanPlugins().then(setData).catch((e: Error) => setError(e.message))}
          >
            Rescan
          </button>
        </div>
        {notice && (
          <p className={`text-sm ${notice.ok ? "text-emerald-400" : "text-red-400"}`}>{notice.text}</p>
        )}
        {error && <p className="text-sm text-red-400">{error}</p>}
        {data?.plugins.map((p) => <PluginCard key={p.name} plugin={p} onChange={replace} />)}
        {mcp.length > 0 && <McpSection servers={mcp} onChange={setMcp} />}
        {data &&
          Object.entries(data.invalid).map(([path, problem]) => (
            <div key={path} className="card border-red-900">
              <p className="break-all text-sm font-medium">{path}</p>
              <p className="text-sm text-red-300">Could not load: {problem}</p>
            </div>
          ))}
      </div>
    </div>
  );
}

function PluginCard({ plugin, onChange }: { plugin: PluginInfo; onChange: (p: PluginInfo) => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const status = STATUS[plugin.status];
  const needsAttention = plugin.status === "needs_config" || plugin.status === "needs_auth";

  async function run(action: () => Promise<PluginInfo | void>, done?: string) {
    setBusy(true);
    setMessage(null);
    try {
      const info = await action();
      if (info) onChange(info);
      if (done) setMessage({ ok: true, text: done });
    } catch (e) {
      setMessage({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  async function connect() {
    setBusy(true);
    try {
      location.href = (await api.oauthStart(plugin.name)).url;
    } catch (e) {
      setMessage({ ok: false, text: (e as Error).message });
      setBusy(false);
    }
  }

  return (
    <section className="card space-y-3">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="font-medium">{plugin.name}</h2>
            <span className="text-xs text-slate-500">
              v{plugin.version}
              {plugin.builtin ? " · built-in" : ""}
            </span>
            <span className={`rounded-full px-2 py-0.5 text-xs ${status.className}`}>{status.label}</span>
          </div>
          <p className="mt-1 text-sm text-slate-400">{plugin.description}</p>
        </div>
        <button
          className={plugin.enabled ? "btn-ghost shrink-0" : "btn shrink-0"}
          disabled={busy}
          onClick={() =>
            run(() => (plugin.enabled ? api.disablePlugin(plugin.name) : api.enablePlugin(plugin.name)))
          }
        >
          {plugin.enabled ? "Turn off" : "Turn on"}
        </button>
      </div>

      {plugin.error && (
        <p className={`text-sm ${plugin.status === "error" ? "text-red-300" : "text-amber-300"}`}>
          {plugin.error}
        </p>
      )}

      {plugin.oauth && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          {!plugin.oauth.client_configured ? (
            <span className="text-amber-300">
              Set oauth.{plugin.oauth.provider}.client_id and client_secret in config.yaml (see the
              README), then restart.
            </span>
          ) : plugin.oauth.connected ? (
            <>
              <span className="text-slate-300">Account connected</span>
              <button className="btn-ghost" disabled={busy} onClick={connect}>
                Reconnect
              </button>
              <button
                className="btn-ghost"
                disabled={busy}
                onClick={() => run(() => api.oauthDisconnect(plugin.name))}
              >
                Disconnect
              </button>
            </>
          ) : (
            <button className="btn" disabled={busy} onClick={connect}>
              Connect <span className="mx-1 capitalize">{plugin.oauth.provider}</span> account
            </button>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <button className="btn-ghost" onClick={() => setOpen(!open)}>
          {open ? "Hide details" : needsAttention ? "Settings (needed)" : "Settings and details"}
        </button>
        {plugin.status === "running" && plugin.capabilities.includes("data_source") && (
          <button
            className="btn-ghost"
            disabled={busy}
            onClick={() =>
              run(async () => {
                const result = await api.syncPlugin(plugin.name);
                if (!result.ok) throw new Error(result.error ?? "Sync failed");
                return result;
              }, "Synced.")
            }
          >
            {busy ? "Working…" : "Sync now"}
          </button>
        )}
        {plugin.last_sync_at && (
          <span className="text-xs text-slate-500">
            Last sync {new Date(plugin.last_sync_at).toLocaleString()}
          </span>
        )}
      </div>
      {message && (
        <p className={`text-sm ${message.ok ? "text-emerald-400" : "text-red-400"}`}>{message.text}</p>
      )}

      {open && <Details plugin={plugin} busy={busy} run={run} />}
    </section>
  );
}

function Details({
  plugin,
  busy,
  run,
}: {
  plugin: PluginInfo;
  busy: boolean;
  run: (action: () => Promise<PluginInfo | void>, done?: string) => Promise<void>;
}) {
  const [config, setConfig] = useState<Record<string, unknown>>(plugin.config);
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const properties = Object.entries(plugin.config_schema.properties ?? {});
  const secretNames = Object.keys(plugin.secrets);
  const perms = plugin.permissions;

  function save() {
    const cleaned = Object.fromEntries(
      Object.entries(config).filter(([, v]) => v !== "" && v !== null && v !== undefined),
    );
    const changedSecrets = Object.fromEntries(Object.entries(secrets).filter(([, v]) => v !== ""));
    return run(async () => {
      const info = await api.configurePlugin(plugin.name, cleaned, changedSecrets);
      setSecrets({});
      return info;
    }, "Saved.");
  }

  return (
    <div className="space-y-4 border-t border-slate-800 pt-3">
      {(properties.length > 0 || secretNames.length > 0) && (
        <div className="space-y-3">
          {properties.map(([key, prop]) => (
            <Field
              key={key}
              name={key}
              prop={prop}
              required={plugin.config_schema.required?.includes(key) ?? false}
              value={config[key]}
              onChange={(v) => setConfig((c) => ({ ...c, [key]: v }))}
            />
          ))}
          {secretNames.map((name) => (
            <label key={name} className="block text-xs text-slate-400">
              {name} (secret)
              <input
                className="input mt-1"
                type="password"
                autoComplete="off"
                placeholder={plugin.secrets[name] ? "saved — type to replace" : "not set"}
                value={secrets[name] ?? ""}
                onChange={(e) => setSecrets((s) => ({ ...s, [name]: e.target.value }))}
              />
            </label>
          ))}
          <button className="btn" disabled={busy} onClick={save}>
            Save settings
          </button>
        </div>
      )}

      <dl className="space-y-1 text-xs text-slate-400">
        <Row label="Can do">{plugin.capabilities.join(", ") || "nothing"}</Row>
        <Row label="Network access">{perms.network.join(", ") || "none"}</Row>
        <Row label="Writes data">{perms.data_write.join(", ") || "none"}</Row>
        {perms.data_read && <Row label="Reads data">items from all plugins</Row>}
        {plugin.oauth && (
          <Row label="Account permissions">
            {plugin.oauth.requested_scopes.map((s) => s.split("/").pop()).join(", ")}
          </Row>
        )}
        {plugin.tools.length > 0 && <Row label="Tools">{plugin.tools.join(", ")}</Row>}
        {plugin.webhook_url && (
          <Row label="Webhook URL (keep secret)">
            <span className="break-all font-mono">{plugin.webhook_url}</span>
          </Row>
        )}
      </dl>

      <button
        className="text-xs text-red-400 hover:underline"
        disabled={busy}
        onClick={() => {
          if (confirm(`Delete everything ${plugin.name} has synced? This cannot be undone.`)) {
            run(async () => {
              await api.purgePlugin(plugin.name);
            }, "Synced data deleted.");
          }
        }}
      >
        Delete synced data
      </button>
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex gap-2">
      <dt className="w-40 shrink-0 text-slate-500">{label}</dt>
      <dd className="min-w-0">{children}</dd>
    </div>
  );
}

function Field({
  name,
  prop,
  required,
  value,
  onChange,
}: {
  name: string;
  prop: SchemaProperty;
  required: boolean;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const label = (prop.title ?? name) + (required ? " *" : "");
  const hint = prop.description && <span className="mt-1 block text-slate-500">{prop.description}</span>;

  if (prop.type === "boolean") {
    return (
      <label className="flex items-start gap-2 text-sm text-slate-200">
        <input
          type="checkbox"
          className="mt-1"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span>
          {label}
          <span className="text-xs">{hint}</span>
        </span>
      </label>
    );
  }
  if (prop.enum) {
    return (
      <label className="block text-xs text-slate-400">
        {label}
        <select className="input mt-1" value={String(value ?? "")} onChange={(e) => onChange(e.target.value)}>
          {prop.enum.map((option) => (
            <option key={String(option)}>{String(option)}</option>
          ))}
        </select>
        {hint}
      </label>
    );
  }
  if (prop.type === "array") {
    const list = Array.isArray(value) ? (value as unknown[]) : [];
    return (
      <label className="block text-xs text-slate-400">
        {label} (comma separated)
        <input
          className="input mt-1"
          defaultValue={list.join(", ")}
          onChange={(e) =>
            onChange(
              e.target.value
                .split(",")
                .map((x) => x.trim())
                .filter(Boolean),
            )
          }
        />
        {hint}
      </label>
    );
  }
  const numeric = prop.type === "integer" || prop.type === "number";
  return (
    <label className="block text-xs text-slate-400">
      {label}
      <input
        className="input mt-1"
        type={numeric ? "number" : "text"}
        step={prop.type === "number" ? "any" : undefined}
        value={value === undefined || value === null ? "" : String(value)}
        onChange={(e) => onChange(numeric && e.target.value !== "" ? Number(e.target.value) : e.target.value)}
      />
      {hint}
    </label>
  );
}

const MCP_STATUS: Record<McpServer["status"], { label: string; className: string }> = {
  running: { label: "Connected", className: "bg-emerald-950 text-emerald-300" },
  connecting: { label: "Connecting", className: "bg-slate-800 text-slate-300" },
  disabled: { label: "Off", className: "bg-slate-800 text-slate-400" },
  error: { label: "Not connected", className: "bg-red-950 text-red-300" },
};

const APPROVAL: Record<string, string> = {
  never: "runs without asking",
  when_outside_content: "asks when outside content is involved",
  always: "asks every time",
};

function McpSection({ servers, onChange }: { servers: McpServer[]; onChange: (s: McpServer[]) => void }) {
  const [error, setError] = useState("");
  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-lg font-semibold">MCP servers</h2>
        <p className="text-sm text-slate-400">
          Extra tools for the assistant. Servers are added in config.yaml; whatever they return is treated as
          untrusted, and most calls wait for your approval.
        </p>
      </div>
      {error && <p className="text-sm text-red-400">{error}</p>}
      {servers.map((s) => (
        <div key={s.name} className="card space-y-2">
          <div className="flex items-start gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="font-medium">{s.name}</h3>
                <span className="break-all text-xs text-slate-500">{s.where}</span>
                <span className={`rounded-full px-2 py-0.5 text-xs ${MCP_STATUS[s.status].className}`}>
                  {MCP_STATUS[s.status].label}
                </span>
              </div>
              {s.error && <p className="mt-1 text-sm text-red-300">{s.error}</p>}
            </div>
            {s.status !== "disabled" && (
              <button
                className="btn-ghost shrink-0"
                onClick={() =>
                  api
                    .reconnectMcp(s.name)
                    .then((r) => onChange(r.servers))
                    .catch((e: Error) => setError(e.message))
                }
              >
                Reconnect
              </button>
            )}
          </div>
          {s.tools.length > 0 && (
            <ul className="space-y-0.5 text-sm">
              {s.tools.map((t) => (
                <li key={t.name} className="flex flex-wrap justify-between gap-x-3">
                  <span className="font-mono text-slate-300">{t.tool}</span>
                  <span className="text-slate-500">{APPROVAL[t.approval]}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </section>
  );
}
