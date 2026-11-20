import { useEffect, useState } from "react";
import { api, type PhoneNotification } from "../api";

/** What the Android app has forwarded: shown so you can see, and delete, what is kept. */
export default function PhoneNotifications({ enabled }: { enabled: boolean }) {
  const [items, setItems] = useState<PhoneNotification[] | null>(null);
  const [error, setError] = useState("");

  async function load() {
    try {
      setItems((await api.phoneNotifications()).notifications);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }
  useEffect(() => {
    load();
  }, []);

  async function remove(id: number) {
    try {
      await api.deletePhoneNotification(id);
      setItems((list) => (list ?? []).filter((n) => n.id !== id));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function clear() {
    if (!window.confirm("Delete every stored phone notification?")) return;
    try {
      await api.clearPhoneNotifications();
      setItems([]);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="space-y-2 border-t border-slate-800 pt-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium">Stored phone notifications</h3>
        {items && items.length > 0 && (
          <button type="button" className="text-xs text-rose-400 hover:underline" onClick={clear}>
            Delete all
          </button>
        )}
      </div>
      {!enabled && <p className="text-xs text-amber-400">Turned off: the server refuses new ones.</p>}
      {error && <p className="text-xs text-rose-400">{error}</p>}
      {items && items.length === 0 && (
        <p className="text-xs text-slate-500">
          None yet. In the Android app, open More → Phone notifications and choose the apps to send.
        </p>
      )}
      <ul className="max-h-72 space-y-2 overflow-y-auto">
        {(items ?? []).map((n) => (
          <li key={n.id} className="rounded border border-slate-800 p-2 text-xs">
            <div className="flex justify-between gap-2 text-slate-500">
              <span>
                {n.app} · {n.posted_at ? new Date(n.posted_at).toLocaleString() : ""}
              </span>
              <button type="button" className="hover:text-rose-400" onClick={() => remove(n.id)}>
                delete
              </button>
            </div>
            <div className="break-words text-slate-300">{n.title}</div>
            {n.text && <div className="whitespace-pre-wrap break-words text-slate-400">{n.text}</div>}
          </li>
        ))}
      </ul>
    </div>
  );
}
