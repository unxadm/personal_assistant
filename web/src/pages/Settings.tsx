import { useEffect, useState } from "react";
import {
  api,
  type ChannelStatus,
  type LLMSlot,
  type Settings,
  type SettingValue,
  type SettingValues,
  type TestResult,
} from "../api";
import Security from "./Security";
import { disablePush, enablePush, pushState, type PushState } from "../push";

const CATEGORIES: { id: string; label: string }[] = [
  { id: "", label: "Everything else" },
  { id: "reminder", label: "Reminders" },
  { id: "briefing", label: "Daily briefing" },
  { id: "suggestion", label: "Suggestions" },
  { id: "plugin", label: "From plugins" },
];
const PROACTIVE: { id: string; label: string; hint: string }[] = [
  { id: "bill", label: "Bills", hint: "Reminder before the due date" },
  { id: "deadline", label: "Deadlines", hint: "Reminder before the date" },
  { id: "appointment", label: "Appointments", hint: "Calendar event (always asks first)" },
  { id: "commitment", label: "Things you were asked to do", hint: "Reminder" },
  { id: "travel", label: "Travel bookings", hint: "Check-in reminder" },
  { id: "needs_reply", label: "Mail waiting for a reply", hint: "Offers to draft one" },
  { id: "package", label: "Packages", hint: "Tracking details" },
  { id: "calendar_conflict", label: "Calendar conflicts", hint: "Warning" },
  { id: "back_to_back", label: "No travel time between meetings", hint: "Warning" },
  { id: "early_meeting", label: "Early meetings", hint: "Heads-up the evening before" },
  { id: "meeting_brief", label: "Meeting briefs", hint: "Who is coming and recent mail with them" },
];
const INFO_ONLY = ["package", "calendar_conflict", "back_to_back", "early_meeting", "meeting_brief"];
const PUSH_TEXT: Record<PushState, string> = {
  on: "Notifications are on for this device.",
  off: "Notifications are off for this device.",
  denied: "Notifications are blocked for this site in your browser settings.",
  unsupported:
    "This browser cannot receive push here. It needs HTTPS (or localhost); on iPhone, add the app to the Home Screen first.",
};

interface SlotDef {
  id: string; // slot name used by the test endpoint
  path: string; // config path prefix
  title: string;
  hint: string;
  sampling: boolean;
}

const SLOTS: SlotDef[] = [
  { id: "chat", path: "llm.chat", title: "Chat / reasoning", hint: "Main model. Required.", sampling: true },
  {
    id: "fallback",
    path: "llm.chat.fallback",
    title: "Fallback",
    hint: "Used when the chat model keeps failing. Optional.",
    sampling: false,
  },
  {
    id: "embeddings",
    path: "llm.embeddings",
    title: "Embeddings",
    hint: "Enables semantic search over your data. Optional.",
    sampling: false,
  },
  {
    id: "vision",
    path: "llm.vision",
    title: "Vision",
    hint: "Reads photos and screenshots. Leave empty if the chat model can see images itself.",
    sampling: false,
  },
  {
    id: "stt",
    path: "llm.stt",
    title: "Speech-to-text",
    hint: "Turns on the Speak button and Telegram voice notes. Any Whisper-compatible server works, including a local one.",
    sampling: false,
  },
  {
    id: "tts",
    path: "llm.tts",
    title: "Text-to-speech",
    hint: "Optional. Adds a Listen button to replies.",
    sampling: false,
  },
];

function slotOf(settings: Settings, def: SlotDef): Partial<LLMSlot> {
  if (def.id === "fallback") return settings.llm.chat.fallback ?? {};
  return settings.llm[def.id as "chat"];
}

export default function SettingsPage() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [edits, setEdits] = useState<SettingValues>({});
  const [results, setResults] = useState<Record<string, TestResult | "testing">>({});
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [channels, setChannels] = useState<ChannelStatus | null>(null);
  const [push, setPush] = useState<PushState>("off");
  const [tests, setTests] = useState<Record<string, string>>({});

  const loadChannels = () =>
    api
      .channels()
      .then(setChannels)
      .catch((e: Error) => setError(e.message));

  useEffect(() => {
    api
      .settings()
      .then(setSettings)
      .catch((e: Error) => setError(e.message));
    loadChannels();
    pushState().then(setPush);
  }, []);

  if (!settings) return <p className="p-4 text-sm text-red-400">{error}</p>;

  const dirty = Object.keys(edits).length > 0;
  const set = (path: string, value: SettingValue) => {
    setStatus("");
    setEdits((e) => ({ ...e, [path]: value }));
  };

  async function save() {
    setError("");
    try {
      setSettings(await api.saveSettings(edits));
      setEdits({});
      setStatus("Saved. Changes apply immediately.");
      loadChannels();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function test(def: SlotDef) {
    setResults((r) => ({ ...r, [def.id]: "testing" }));
    const llmEdits = Object.fromEntries(Object.entries(edits).filter(([k]) => k.startsWith("llm.")));
    try {
      const result = await api.testLLM(def.id, llmEdits);
      setResults((r) => ({ ...r, [def.id]: result }));
    } catch (e) {
      setResults((r) => ({ ...r, [def.id]: { ok: false, slot: def.id, error: (e as Error).message } }));
    }
  }

  function field(path: string, current: string | number | null | undefined, opts: {
    label: string;
    placeholder?: string;
    type?: "text" | "password" | "number" | "time";
  }) {
    const edited = path in edits;
    const value = edited ? (edits[path] ?? "") : (current ?? "");
    const secret = opts.type === "password";
    return (
      <label className="block text-xs text-slate-400">
        <span className="flex justify-between">
          {opts.label}
          {settings!.overridden.includes(path) && (
            <button
              type="button"
              className="text-sky-400 hover:underline"
              title="Remove the saved override and use the value from config.yaml / environment"
              onClick={() => set(path, null)}
            >
              reset
            </button>
          )}
        </span>
        <input
          className="input mt-1"
          type={secret ? "text" : (opts.type ?? "text")}
          autoComplete="off"
          // Stored keys are only shown masked; typing replaces them.
          placeholder={secret && current ? `${current} (unchanged)` : opts.placeholder}
          value={secret && !edited ? "" : String(value)}
          onChange={(e) => {
            const raw = e.target.value;
            set(path, opts.type === "number" && raw !== "" ? Number(raw) : raw);
          }}
        />
      </label>
    );
  }

  function toggle(path: string, current: boolean, label: string) {
    const checked = path in edits ? Boolean(edits[path]) : current;
    return (
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={checked} onChange={(e) => set(path, e.target.checked)} />
        {label}
      </label>
    );
  }

  async function togglePush() {
    setError("");
    try {
      setPush(push === "on" ? await disablePush() : await enablePush(channels!.vapid_public_key));
      loadChannels();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function testChannel(name: string) {
    setTests((t) => ({ ...t, [name]: "Sending…" }));
    try {
      const result = await api.testChannel(name);
      setTests((t) => ({ ...t, [name]: result.ok ? "Sent — check that it arrived." : `Failed: ${result.error}` }));
    } catch (e) {
      setTests((t) => ({ ...t, [name]: `Failed: ${(e as Error).message}` }));
    }
  }

  const pro = settings.proactive;
  const levels = (edits["proactive.levels"] as Record<string, string> | undefined) ?? pro.levels;

  // Which channels each kind of notification uses. "" is the default row.
  const n = settings.notifications;
  const defaultChannels = (edits["notifications.default_channels"] as string[] | undefined) ?? n.default_channels;
  const categories = (edits["notifications.categories"] as Record<string, string[]> | undefined) ?? n.categories;
  const routeOf = (category: string) => (category ? (categories[category] ?? defaultChannels) : defaultChannels);
  function setRoute(category: string, channel: string, on: boolean) {
    const next = on ? [...routeOf(category), channel] : routeOf(category).filter((c) => c !== channel);
    if (category) set("notifications.categories", { ...categories, [category]: next });
    else set("notifications.default_channels", next);
  }
  function resetRoute(category: string) {
    const rest = { ...categories };
    delete rest[category];
    set("notifications.categories", rest);
  }

  function listField(path: string, current: string[], label: string, placeholder: string) {
    return (
      <label className="block text-xs text-slate-400">
        {label}
        <input
          key={current.join(",")}
          className="input mt-1"
          placeholder={placeholder}
          defaultValue={current.join(", ")}
          onChange={(e) =>
            set(
              path,
              e.target.value
                .split(",")
                .map((x) => x.trim())
                .filter(Boolean),
            )
          }
        />
      </label>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl space-y-4 p-4 pb-24">
        <div>
          <h1 className="text-lg font-semibold">Settings</h1>
          <p className="text-sm text-slate-400">
            Any OpenAI-compatible endpoint works. Values saved here override config.yaml and
            environment variables. Empty endpoint or key fields inherit from the chat model.
          </p>
        </div>

        {SLOTS.map((def) => {
          const slot = slotOf(settings, def);
          const result = results[def.id];
          return (
            <section key={def.id} className="card space-y-3">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h2 className="font-medium">{def.title}</h2>
                  <p className="text-xs text-slate-500">{def.hint}</p>
                </div>
                <button className="btn-ghost shrink-0" disabled={result === "testing"} onClick={() => test(def)}>
                  {result === "testing" ? "Testing…" : "Test connection"}
                </button>
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                {field(`${def.path}.model`, slot.model, { label: "Model", placeholder: "e.g. gpt-4o-mini, llama3.1" })}
                {field(`${def.path}.base_url`, slot.base_url, {
                  label: "Base URL",
                  placeholder: def.id === "chat" ? "https://api.openai.com/v1" : "same as chat",
                })}
                {field(`${def.path}.api_key`, slot.api_key, {
                  label: "API key",
                  type: "password",
                  placeholder: def.id === "chat" ? "not needed for local servers" : "same as chat",
                })}
                {field(`${def.path}.timeout`, slot.timeout, { label: "Timeout (seconds)", type: "number" })}
                {def.id === "stt" &&
                  field("llm.stt.language", slot.language, {
                    label: "Language hint",
                    placeholder: "auto-detect, or e.g. en, no, de",
                  })}
                {def.id === "tts" && field("llm.tts.voice", slot.voice, { label: "Voice", placeholder: "alloy" })}
                {def.sampling && (
                  <>
                    {field(`${def.path}.temperature`, slot.temperature, {
                      label: "Temperature",
                      type: "number",
                      placeholder: "provider default",
                    })}
                    {field(`${def.path}.max_tokens`, slot.max_tokens, {
                      label: "Max tokens",
                      type: "number",
                      placeholder: "provider default",
                    })}
                  </>
                )}
              </div>
              {result && result !== "testing" && (
                <p className={`text-sm ${result.ok ? "text-emerald-400" : "text-red-400"}`}>
                  {result.ok
                    ? `Connected to ${result.model} in ${result.latency_ms} ms — ${result.detail}`
                    : `Failed: ${result.error}`}
                </p>
              )}
            </section>
          );
        })}

        <section className="card space-y-3">
          <h2 className="font-medium">General</h2>
          <div className="grid gap-3 sm:grid-cols-2">
            {field("timezone", settings.timezone, { label: "Timezone", placeholder: "e.g. Europe/Oslo" })}
            {field("agent.name", settings.agent.name, { label: "Assistant name" })}
          </div>
          <div className="space-y-3 border-t border-slate-800 pt-3">
            <div>
              <h3 className="text-sm font-medium">Keep out of AI processing</h3>
              <p className="text-xs text-slate-500">
                Matching data is never stored, searched or shown to the model. Saving also removes
                matching data that is already stored. Separate entries with commas.
              </p>
            </div>
            {listField("privacy.exclude_senders", settings.privacy.exclude_senders, "Senders (addresses or domains)", "boss@example.com, bank.example")}
            {listField("privacy.exclude_labels", settings.privacy.exclude_labels, "Email labels", "Private, Medical")}
            {listField("privacy.exclude_calendars", settings.privacy.exclude_calendars, "Calendar IDs", "family@group.calendar.google.com")}
          </div>
          <p className="text-xs text-slate-500">
            Semantic search:{" "}
            {settings.vector_search.enabled
              ? `on (${settings.vector_search.backend})`
              : "off — set an embeddings model to enable"}
          </p>
        </section>

        <section className="card space-y-4">
          <div>
            <h2 className="font-medium">Notifications</h2>
            <p className="text-xs text-slate-500">
              Where reminders and briefings are delivered. Email and Telegram messages pass through
              those services; browser push is encrypted end to end.
            </p>
          </div>

          <div className="space-y-2">
            <h3 className="text-sm font-medium">Browser / phone push</h3>
            <div className="flex flex-wrap items-center gap-3">
              <button
                className="btn-ghost"
                disabled={!channels || push === "unsupported" || push === "denied"}
                onClick={togglePush}
              >
                {push === "on" ? "Turn off on this device" : "Turn on for this device"}
              </button>
              <span className="text-xs text-slate-400">{PUSH_TEXT[push]}</span>
            </div>
          </div>

          <div className="space-y-3 border-t border-slate-800 pt-3">
            <h3 className="text-sm font-medium">Telegram</h3>
            {toggle("notifications.telegram.enabled", n.telegram.enabled, "Use Telegram")}
            <div className="grid gap-3 sm:grid-cols-2">
              {field("notifications.telegram.bot_token", n.telegram.bot_token, {
                label: "Bot token (from @BotFather)",
                type: "password",
              })}
              {field("notifications.telegram.chat_id", n.telegram.chat_id, {
                label: "Your chat ID",
                placeholder: "save the token, then send /start to your bot",
              })}
            </div>
            {toggle("notifications.telegram.chat", n.telegram.chat, "Let me chat with the assistant in Telegram")}
          </div>

          <div className="space-y-3 border-t border-slate-800 pt-3">
            <h3 className="text-sm font-medium">Email</h3>
            {toggle("notifications.email.enabled", n.email.enabled, "Email me through Gmail")}
            <p className="text-xs text-slate-500">
              Sent from your connected Gmail account through the Gmail API; there is no mail server or
              password to enter. On the Plugins page, turn on “Allow sending notification email” for
              Gmail and reconnect the account. That grants a send-only permission, used for these
              notifications alone: the assistant still cannot send mail in a conversation.
            </p>
            <div className="grid gap-3 sm:grid-cols-2">
              {field("notifications.email.to", n.email.to, {
                label: "Send to",
                placeholder: "your Gmail address (leave empty)",
              })}
            </div>
          </div>

          {channels && (
            <div className="space-y-2 border-t border-slate-800 pt-3">
              <h3 className="text-sm font-medium">Status</h3>
              <ul className="space-y-2">
                {channels.channels.map((ch) => (
                  <li key={ch.name} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
                    <span className={ch.ready ? "text-emerald-400" : "text-slate-500"}>
                      {ch.ready ? "Ready" : "Not set up"}
                    </span>
                    <span>{ch.label}</span>
                    <span className="text-xs text-slate-500">{ch.detail}</span>
                    {ch.ready && (
                      <button className="btn-ghost ml-auto py-1" disabled={dirty} onClick={() => testChannel(ch.name)}>
                        Send test
                      </button>
                    )}
                    {tests[ch.name] && <span className="w-full text-xs text-slate-400">{tests[ch.name]}</span>}
                  </li>
                ))}
              </ul>
              {dirty && <p className="text-xs text-slate-500">Save your changes before sending a test.</p>}

              <h3 className="pt-2 text-sm font-medium">What goes where</h3>
              <div className="overflow-x-auto">
                <table className="text-sm">
                  <thead>
                    <tr className="text-left text-xs text-slate-400">
                      <th className="pr-4 font-normal" />
                      {channels.channels.map((ch) => (
                        <th key={ch.name} className="pr-4 font-normal">
                          {ch.label}
                        </th>
                      ))}
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {CATEGORIES.map((cat) => (
                      <tr key={cat.id}>
                        <td className="py-1 pr-4">{cat.label}</td>
                        {channels.channels.map((ch) => (
                          <td key={ch.name} className="pr-4">
                            <input
                              type="checkbox"
                              aria-label={`${cat.label} via ${ch.label}`}
                              checked={routeOf(cat.id).includes(ch.name)}
                              onChange={(e) => setRoute(cat.id, ch.name, e.target.checked)}
                            />
                          </td>
                        ))}
                        <td className="text-xs">
                          {cat.id && categories[cat.id] ? (
                            <button className="text-sky-400 hover:underline" onClick={() => resetRoute(cat.id)}>
                              same as “Everything else”
                            </button>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="text-xs text-slate-500">
                If none of the chosen channels works, the others are tried so a reminder is not lost.
              </p>
            </div>
          )}

          <div className="space-y-3 border-t border-slate-800 pt-3">
            <h3 className="text-sm font-medium">Quiet hours</h3>
            {toggle(
              "notifications.quiet_hours.enabled",
              n.quiet_hours.enabled,
              "Hold notifications during quiet hours and deliver them when they end",
            )}
            <div className="grid gap-3 sm:grid-cols-3">
              {field("notifications.quiet_hours.start", n.quiet_hours.start, { label: "From", type: "time" })}
              {field("notifications.quiet_hours.end", n.quiet_hours.end, { label: "Until", type: "time" })}
              {field("notifications.default_snooze_minutes", n.default_snooze_minutes, {
                label: "Snooze length (minutes)",
                type: "number",
              })}
            </div>
            <p className="text-xs text-slate-500">
              Reminders you mark as urgent (“…and wake me if you have to”) still come through.
            </p>
          </div>

          <div className="space-y-3 border-t border-slate-800 pt-3">
            <h3 className="text-sm font-medium">Daily briefing</h3>
            {toggle("briefing.enabled", settings.briefing.enabled, "Write a briefing of my day every morning (read it on the Briefing page)")}
            <div className="grid gap-3 sm:grid-cols-3">
              {field("briefing.time", settings.briefing.time, { label: "At", type: "time" })}
              <label className="block text-xs text-slate-400 sm:col-span-2">
                Length
                <select
                  className="input mt-1"
                  value={String(edits["briefing.detail"] ?? settings.briefing.detail)}
                  onChange={(e) => set("briefing.detail", e.target.value)}
                >
                  <option value="detailed">Detailed: a few hundred words, with tomorrow and what to do first</option>
                  <option value="short">Short: 150 words at most</option>
                </select>
              </label>
            </div>
          </div>
        </section>

        <section className="card space-y-4">
          <div>
            <h2 className="font-medium">Proactive suggestions</h2>
            <p className="text-xs text-slate-500">
              The assistant watches new mail and your calendar and files what it notices in the Inbox. Mail that
              looks like a bill, deadline or request is sent to your chat model to be read; everything else is
              checked by fixed rules.
            </p>
          </div>
          {toggle("proactive.enabled", pro.enabled, "Look for things worth my attention")}
          <div className="space-y-2">
            {PROACTIVE.map((cat) => (
              <label key={cat.id} className="flex flex-wrap items-center justify-between gap-2 text-sm">
                <span>
                  {cat.label} <span className="text-xs text-slate-500">· {cat.hint}</span>
                </span>
                <select
                  className="input w-auto"
                  value={levels[cat.id]}
                  onChange={(e) => set("proactive.levels", { ...levels, [cat.id]: e.target.value })}
                >
                  <option value="off">Off</option>
                  <option value="notify">Tell me only</option>
                  {!INFO_ONLY.includes(cat.id) && <option value="suggest">Suggest and wait</option>}
                  {pro.auto_categories.includes(cat.id) && <option value="auto">Do it automatically</option>}
                </select>
              </label>
            ))}
            <p className="text-xs text-slate-500">
              “Do it automatically” only ever creates a reminder, which you can undo under Activity. Calendar
              events, drafts and anything that reaches other people always wait for you.
            </p>
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            {field("proactive.max_notifications_per_hour", pro.max_notifications_per_hour, {
              label: "Notifications per hour, at most",
              type: "number",
            })}
            {field("proactive.max_notifications_per_day", pro.max_notifications_per_day, {
              label: "Notifications per day, at most",
              type: "number",
            })}
            {field("proactive.max_llm_calls_per_hour", pro.max_llm_calls_per_hour, {
              label: "Emails read by the model per hour, at most",
              type: "number",
            })}
            {field("proactive.lead_hours", pro.lead_hours, {
              label: "Remind me this many hours before a due date",
              type: "number",
            })}
            {field("proactive.early_before", pro.early_before, { label: "A meeting is early before", type: "time" })}
            {field("proactive.brief_minutes", pro.brief_minutes, {
              label: "Meeting brief, minutes before (0 = never)",
              type: "number",
            })}
          </div>
          <p className="text-xs text-slate-500">
            Suggestions beyond the notification limit are not lost; they wait in the Inbox without a ping.
          </p>
        </section>

        <Security />

        <div className="fixed inset-x-0 bottom-0 border-t border-slate-800 bg-slate-950/95 p-3">
          <div className="mx-auto flex max-w-3xl items-center gap-3">
            <button className="btn" disabled={!dirty} onClick={save}>
              Save changes
            </button>
            {dirty && (
              <button className="btn-ghost" onClick={() => setEdits({})}>
                Discard
              </button>
            )}
            <span className={`text-sm ${error ? "text-red-400" : "text-slate-400"}`}>{error || status}</span>
          </div>
        </div>
      </div>
    </div>
  );
}
