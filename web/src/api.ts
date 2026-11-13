export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let message = res.statusText;
    try {
      const data = await res.json();
      message = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch {
      /* non-JSON error body */
    }
    if (res.status === 401 && !path.startsWith("/auth/")) {
      window.dispatchEvent(new Event("pa:unauthorized"));
    }
    throw new ApiError(res.status, message);
  }
  return res.json() as Promise<T>;
}

async function send<T>(path: string, body: Blob, headers: Record<string, string>): Promise<T> {
  // Raw-body upload: the file is the request body.
  const res = await fetch(`/api${path}`, { method: "POST", credentials: "same-origin", headers, body });
  if (!res.ok) {
    const data = await res.json().catch(() => ({ detail: res.statusText }));
    if (res.status === 401) window.dispatchEvent(new Event("pa:unauthorized"));
    throw new ApiError(res.status, typeof data.detail === "string" ? data.detail : res.statusText);
  }
  return res.json() as Promise<T>;
}

export interface AttachmentInfo {
  id: number;
  mime: string;
  filename: string;
  size: number;
}
export interface AuthStatus {
  setup_required: boolean;
  setup_code_required: boolean;
  authenticated: boolean;
}
export interface ConversationInfo {
  id: number;
  title: string;
  channel: string;
  updated_at: string;
}
export interface ToolEvent {
  tool: string;
  arguments: Record<string, unknown>;
  ok: boolean;
  pending_action_id?: number;
}
export interface PendingAction {
  id: number;
  conversation_id: number | null;
  tool: string;
  arguments: Record<string, unknown>;
  status: "pending" | "running" | "done" | "failed" | "rejected";
  result: string | null;
  created_at: string;
}
export interface SchemaProperty {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  items?: { type?: string };
}
export interface PluginInfo {
  name: string;
  version: string;
  description: string;
  builtin: boolean;
  capabilities: string[];
  permissions: { network: string[]; data_write: string[]; data_read: boolean; notify: boolean };
  config_schema: { properties?: Record<string, SchemaProperty>; required?: string[] };
  config: Record<string, unknown>;
  secrets: Record<string, boolean>;
  enabled: boolean;
  status: "disabled" | "running" | "needs_config" | "needs_auth" | "error";
  error: string | null;
  last_sync_at: string | null;
  sync_interval: number | null;
  tools: string[];
  oauth: {
    provider: string | null;
    connected: boolean;
    client_configured: boolean;
    requested_scopes: string[];
    granted_scopes: string[];
  } | null;
  webhook_url: string | null;
}
export interface PluginList {
  plugins: PluginInfo[];
  invalid: Record<string, string>;
}
export interface ChatMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  meta?: {
    model?: string;
    used_fallback?: boolean;
    tool_events?: ToolEvent[];
    attachments?: AttachmentInfo[];
  };
}
export interface Fact {
  id: number;
  content: string;
  category: string;
  source: string;
  updated_at: string;
}
export interface LLMSlot {
  base_url: string | null;
  api_key: string | null;
  model: string | null;
  temperature: number | null;
  max_tokens: number | null;
  timeout: number;
  tool_mode?: string;
  fallback?: LLMSlot | null;
  language?: string | null;
  voice?: string;
}
export interface Settings {
  timezone: string;
  llm: Record<"chat" | "vision" | "stt" | "tts" | "embeddings", LLMSlot> & {
    max_retries: number;
  };
  agent: Record<string, string | number>;
  privacy: { exclude_labels: string[]; exclude_senders: string[]; exclude_calendars: string[] };
  notifications: {
    web_push: { enabled: boolean; contact: string | null };
    email: {
      enabled: boolean;
      host: string | null;
      port: number;
      security: string;
      username: string | null;
      password: string | null;
      from_addr: string | null;
      to: string | null;
    };
    telegram: { enabled: boolean; bot_token: string | null; chat_id: string | null; chat: boolean };
    default_channels: string[];
    categories: Record<string, string[]>;
    quiet_hours: { enabled: boolean; start: string; end: string };
    default_snooze_minutes: number;
  };
  briefing: { enabled: boolean; time: string };
  proactive: {
    enabled: boolean;
    levels: Record<string, Level>;
    auto_categories: string[];
    min_confidence: number;
    notify_confidence: number;
    max_notifications_per_hour: number;
    max_notifications_per_day: number;
    max_llm_calls_per_hour: number;
    lookback_days: number;
    lead_hours: number;
    travel_gap_minutes: number;
    early_before: string;
    brief_minutes: number;
    reply_after_days: number;
  };
  capabilities: { stt: boolean; tts: boolean };
  overridden: string[];
  vector_search: { enabled: boolean; backend: string };
}
export type Level = "off" | "notify" | "suggest" | "auto";
export interface Suggestion {
  id: number;
  category: string;
  title: string;
  detail: string;
  reason: string;
  confidence: number;
  source: "rule" | "llm";
  proposed: { type: "reminder" | "event" | "chat"; prompt?: string; at?: string; start?: string } | null;
  action_label: string | null;
  url: string | null;
  status: "pending" | "accepted" | "auto_applied" | "dismissed" | "expired";
  feedback: "useful" | "not_useful" | null;
  result: string | null;
  created_at: string;
  resolved_at: string | null;
}
export interface SuggestionList {
  suggestions: Suggestion[];
  pending: number;
  enabled: boolean;
}
export interface Learning {
  categories: Record<string, { useful: number; not_useful: number; bias: number }>;
  muted_senders: string[];
}
export interface AuditEntry {
  id: number;
  at: string;
  actor: "assistant" | "proactive" | "user";
  action: string;
  summary: string;
  change: boolean;
  detail: Record<string, unknown>;
  undo_label: string | null;
  undone_at: string | null;
}
export interface TestResult {
  ok: boolean;
  slot: string;
  model?: string;
  detail?: string;
  error?: string;
  latency_ms?: number;
}
export type SettingValue =
  | string
  | number
  | boolean
  | string[]
  | Record<string, string[]>
  | Record<string, string>
  | null;
export type SettingValues = Record<string, SettingValue>;
export interface Reminder {
  id: number;
  text: string;
  due_at: string;
  due_local: string;
  next_at: string | null;
  snoozed_until: string | null;
  rrule: string | null;
  repeat: string;
  status: "scheduled" | "fired" | "done" | "cancelled";
  urgency: "low" | "normal" | "high";
  source: "user" | "agent" | "external";
}
export interface NotificationInfo {
  id: number;
  category: string;
  title: string;
  body: string;
  status: "held" | "sent" | "failed";
  deliver_after: string | null;
  channels: Record<string, string>;
  created_at: string;
}
export interface ChannelInfo {
  name: string;
  label: string;
  plugin: boolean;
  ready: boolean;
  detail: string;
}
export interface ChannelStatus {
  channels: ChannelInfo[];
  vapid_public_key: string;
  devices: { id: number; label: string }[];
  quiet_until: string | null;
}
export interface Briefing {
  latest: { date: string; text: string; generated_at: string } | null;
}

export const api = {
  authStatus: () => request<AuthStatus>("GET", "/auth/status"),
  setup: (password: string, setup_code = "") => request("POST", "/auth/setup", { password, setup_code }),
  login: (password: string, code = "") => request("POST", "/auth/login", { password, code }),
  security: () =>
    request<{ totp_enabled: boolean; recovery_codes_left: number; sessions: number }>("GET", "/auth/security"),
  changePassword: (current: string, next: string) => request("POST", "/auth/password", { current, new: next }),
  revokeOtherSessions: () => request("POST", "/auth/sessions/revoke-others"),
  totpSetup: () => request<{ secret: string; uri: string }>("POST", "/auth/totp/setup"),
  totpEnable: (code: string) => request<{ recovery_codes: string[] }>("POST", "/auth/totp/enable", { code }),
  totpDisable: (password: string) => request("POST", "/auth/totp/disable", { password }),
  newRecoveryCodes: (password: string) =>
    request<{ recovery_codes: string[] }>("POST", "/auth/totp/recovery-codes", { password }),
  logout: () => request("POST", "/auth/logout"),

  conversations: () => request<ConversationInfo[]>("GET", "/conversations"),
  messages: (id: number) => request<ChatMessage[]>("GET", `/conversations/${id}/messages`),
  deleteConversation: (id: number) => request("DELETE", `/conversations/${id}`),
  chat: (text: string, conversation_id: number | null, attachment_ids: number[] = []) =>
    request<{ conversation_id: number; message: ChatMessage }>("POST", "/chat", {
      text,
      conversation_id,
      attachment_ids,
    }),
  uploadImage: (image: Blob, name: string) =>
    send<AttachmentInfo>("/attachments", image, {
      "Content-Type": image.type || "application/octet-stream",
      "X-Filename": encodeURIComponent(name),
    }),
  transcribe: (audio: Blob) =>
    send<{ text: string }>("/voice/transcribe", audio, {
      "Content-Type": audio.type || "application/octet-stream",
    }),
  speak: async (text: string): Promise<Blob> => {
    const res = await fetch("/api/voice/speak", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text.slice(0, 4000) }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ detail: res.statusText }));
      throw new ApiError(res.status, String(data.detail));
    }
    return res.blob();
  },

  facts: () => request<Fact[]>("GET", "/memory/facts"),
  addFact: (content: string, category: string) =>
    request<Fact>("POST", "/memory/facts", { content, category }),
  updateFact: (id: number, patch: { content?: string; category?: string }) =>
    request<Fact>("PATCH", `/memory/facts/${id}`, patch),
  deleteFact: (id: number) => request("DELETE", `/memory/facts/${id}`),

  plugins: () => request<PluginList>("GET", "/plugins"),
  rescanPlugins: () => request<PluginList>("POST", "/plugins/rescan"),
  enablePlugin: (name: string) => request<PluginInfo>("POST", `/plugins/${name}/enable`),
  disablePlugin: (name: string) => request<PluginInfo>("POST", `/plugins/${name}/disable`),
  configurePlugin: (
    name: string,
    config: Record<string, unknown> | null,
    secrets: Record<string, string | null> | null,
  ) => request<PluginInfo>("PUT", `/plugins/${name}/config`, { config, secrets }),
  syncPlugin: (name: string) => request<PluginInfo & { ok: boolean }>("POST", `/plugins/${name}/sync`),
  purgePlugin: (name: string) => request<{ removed: number }>("DELETE", `/plugins/${name}/data`),
  oauthStart: (name: string) =>
    request<{ url: string; redirect_uri: string }>("GET", `/plugins/${name}/oauth/start`),
  oauthDisconnect: (name: string) => request<PluginInfo>("POST", `/plugins/${name}/oauth/disconnect`),

  actions: (conversationId: number) =>
    request<PendingAction[]>("GET", `/actions?conversation_id=${conversationId}`),
  approveAction: (id: number) => request<PendingAction>("POST", `/actions/${id}/approve`),
  rejectAction: (id: number) => request<PendingAction>("POST", `/actions/${id}/reject`),

  reminders: (all = false) => request<Reminder[]>("GET", `/reminders${all ? "?all=true" : ""}`),
  addReminder: (body: { text: string; at: string; rrule?: string | null }) =>
    request<Reminder>("POST", "/reminders", body),
  quickReminder: (text: string) => request<Reminder>("POST", "/reminders/quick", { text }),
  updateReminder: (id: number, patch: { text?: string; at?: string; rrule?: string }) =>
    request<Reminder>("PATCH", `/reminders/${id}`, patch),
  snoozeReminder: (id: number, minutes?: number) =>
    request<Reminder>("POST", `/reminders/${id}/snooze`, minutes ? { minutes } : {}),
  completeReminder: (id: number) => request<Reminder>("POST", `/reminders/${id}/complete`),
  deleteReminder: (id: number) => request("DELETE", `/reminders/${id}`),

  notifications: () => request<NotificationInfo[]>("GET", "/notifications?limit=20"),
  channels: () => request<ChannelStatus>("GET", "/notifications/channels"),
  testChannel: (channel: string) =>
    request<{ ok: boolean; error?: string }>("POST", "/notifications/test", { channel }),
  pushSubscribe: (body: { endpoint: string; keys: Record<string, string>; label: string }) =>
    request("POST", "/push/subscribe", body),
  pushUnsubscribe: (endpoint: string) => request("POST", "/push/unsubscribe", { endpoint }),
  briefing: () => request<Briefing>("GET", "/briefing"),
  runBriefing: () => request<Briefing>("POST", "/briefing/run"),

  suggestions: (history = false) =>
    request<SuggestionList>("GET", `/suggestions${history ? "?history=true" : ""}`),
  suggestionCount: () => request<{ pending: number }>("GET", "/suggestions/count"),
  learning: () => request<Learning>("GET", "/suggestions/learning"),
  checkNow: () => request<{ created: number }>("POST", "/suggestions/check"),
  acceptSuggestion: (id: number, mode?: "reminder") =>
    request<Suggestion>("POST", `/suggestions/${id}/accept`, mode ? { mode } : {}),
  dismissSuggestion: (id: number) => request<Suggestion>("POST", `/suggestions/${id}/dismiss`),
  suggestionFeedback: (id: number, useful: boolean | null) =>
    request<Suggestion>("POST", `/suggestions/${id}/feedback`, { useful }),
  audit: (changes: boolean, before?: number) =>
    request<AuditEntry[]>("GET", `/audit?changes=${changes}${before ? `&before=${before}` : ""}`),
  undoAudit: (id: number) => request<AuditEntry>("POST", `/audit/${id}/undo`),

  settings: () => request<Settings>("GET", "/settings"),
  saveSettings: (values: SettingValues) => request<Settings>("PATCH", "/settings", { values }),
  testLLM: (slot: string, values: SettingValues) =>
    request<TestResult>("POST", "/settings/llm/test", { slot, values }),
};
