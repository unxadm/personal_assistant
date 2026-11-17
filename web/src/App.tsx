import { useCallback, useEffect, useState } from "react";
import { api, type AuthStatus } from "./api";
import Activity from "./pages/Activity";
import Briefing from "./pages/Briefing";
import Chat from "./pages/Chat";
import Inbox from "./pages/Inbox";
import Login from "./pages/Login";
import Memory from "./pages/Memory";
import Plugins from "./pages/Plugins";
import Reminders from "./pages/Reminders";
import SettingsPage from "./pages/Settings";

type Tab = "chat" | "briefing" | "inbox" | "reminders" | "memory" | "plugins" | "activity" | "settings";
const TABS: { id: Tab; label: string }[] = [
  { id: "chat", label: "Chat" },
  { id: "briefing", label: "Briefing" },
  { id: "inbox", label: "Inbox" },
  { id: "reminders", label: "Reminders" },
  { id: "memory", label: "Memory" },
  { id: "plugins", label: "Plugins" },
  { id: "activity", label: "Activity" },
  { id: "settings", label: "Settings" },
];

export default function App() {
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [error, setError] = useState("");
  // The OAuth callback redirects back with ?tab=plugins&oauth=...; notifications open
  // ?tab=reminders, ?tab=inbox or ?tab=briefing.
  const [tab, setTab] = useState<Tab>(() => {
    const wanted = new URLSearchParams(location.search).get("tab");
    return TABS.some((t) => t.id === wanted) ? (wanted as Tab) : "chat";
  });

  const [waiting, setWaiting] = useState(0);
  // "Draft a reply" in the inbox opens a new chat with the request filled in.
  const [draft, setDraft] = useState("");

  const refreshCount = useCallback(() => {
    api
      .suggestionCount()
      .then((r) => setWaiting(r.pending))
      .catch(() => {});
  }, []);

  const refresh = useCallback(() => {
    api
      .authStatus()
      .then(setAuth)
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    refresh();
    window.addEventListener("pa:unauthorized", refresh);
    return () => window.removeEventListener("pa:unauthorized", refresh);
  }, [refresh]);

  const signedIn = auth?.authenticated === true;
  useEffect(() => {
    if (!signedIn) return;
    refreshCount();
    const timer = setInterval(refreshCount, 60_000);
    return () => clearInterval(timer);
  }, [signedIn, refreshCount]);

  if (error) {
    return <p className="p-6 text-sm text-red-400">Cannot reach the server: {error}</p>;
  }
  if (!auth) return null;
  if (!auth.authenticated) return <Login setup={auth.setup_required} needsSetupCode={auth.setup_code_required} onDone={refresh} />;

  return (
    <div className="flex h-full flex-col">
      <header className="flex shrink-0 items-center gap-1 overflow-x-auto border-b border-slate-800 px-3 py-2">
        <span className="mr-3 hidden font-semibold sm:inline">Assistant</span>
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`shrink-0 rounded-lg px-3 py-1.5 text-sm ${
              tab === t.id ? "bg-slate-800 text-white" : "text-slate-400 hover:text-slate-200"
            }`}
          >
            {t.label}
            {t.id === "inbox" && waiting > 0 && (
              <span className="ml-1.5 rounded-full bg-sky-600 px-1.5 py-0.5 text-xs text-white">{waiting}</span>
            )}
          </button>
        ))}
        <button
          className="ml-auto shrink-0 rounded-lg px-3 py-1.5 text-sm text-slate-400 hover:text-slate-200"
          onClick={() => api.logout().finally(refresh)}
        >
          Sign out
        </button>
      </header>
      <main className="min-h-0 flex-1">
        {tab === "chat" && <Chat draft={draft} onDraftUsed={() => setDraft("")} />}
        {tab === "briefing" && <Briefing onSettings={() => setTab("settings")} />}
        {tab === "inbox" && (
          <Inbox
            onChanged={refreshCount}
            onDraft={(prompt) => {
              setDraft(prompt);
              setTab("chat");
            }}
          />
        )}
        {tab === "activity" && <Activity />}
        {tab === "reminders" && <Reminders />}
        {tab === "memory" && <Memory />}
        {tab === "plugins" && <Plugins />}
        {tab === "settings" && <SettingsPage />}
      </main>
    </div>
  );
}
