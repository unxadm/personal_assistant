import { useCallback, useEffect, useState } from "react";
import { api, type AuditEntry } from "../api";
import { loadZone, stamp } from "../time";

const ACTOR: Record<string, string> = {
  assistant: "Assistant",
  proactive: "Assistant, on its own",
  user: "You",
};

export default function Activity() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [changesOnly, setChangesOnly] = useState(true);
  const [more, setMore] = useState(false);
  const [busy, setBusy] = useState(0);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      await loadZone();
      const rows = await api.audit(changesOnly);
      setEntries(rows);
      setMore(rows.length === 100);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [changesOnly]);

  useEffect(() => {
    load();
  }, [load]);

  async function older() {
    const rows = await api.audit(changesOnly, entries[entries.length - 1].id);
    setEntries((list) => [...list, ...rows]);
    setMore(rows.length === 100);
  }

  async function undo(entry: AuditEntry) {
    setError("");
    setBusy(entry.id);
    try {
      await api.undoAudit(entry.id);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(0);
    }
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl space-y-4 p-4">
        <div>
          <h1 className="font-medium">Activity</h1>
          <p className="text-xs text-slate-500">
            Everything the assistant did on your behalf. Where something can be reversed, there is an undo.
          </p>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={changesOnly} onChange={(e) => setChangesOnly(e.target.checked)} />
          Only things that changed something
        </label>
        {error && <p className="text-sm text-red-400">{error}</p>}
        {entries.length === 0 && <p className="text-sm text-slate-500">Nothing yet.</p>}
        <ul className="space-y-2">
          {entries.map((e) => (
            <li key={e.id} className="card space-y-1">
              <div className="flex items-start justify-between gap-3">
                <p className={`break-words text-sm ${e.undone_at ? "text-slate-500 line-through" : ""}`}>{e.summary}</p>
                {e.undo_label && (
                  <button className="btn-ghost shrink-0" disabled={busy === e.id} onClick={() => undo(e)}>
                    {busy === e.id ? "Undoing…" : `Undo: ${e.undo_label.toLowerCase()}`}
                  </button>
                )}
              </div>
              <p className="text-xs text-slate-400">
                {stamp(e.at)} · {ACTOR[e.actor] ?? e.actor}
                {e.undone_at && " · undone"}
                {e.detail.external_content_involved === true && " · outside content was in play"}
              </p>
              <details className="text-xs text-slate-500">
                <summary className="cursor-pointer">Details</summary>
                <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words">
                  {JSON.stringify(e.detail, null, 2)}
                </pre>
              </details>
            </li>
          ))}
        </ul>
        {more && (
          <button className="btn-ghost" onClick={older}>
            Show older
          </button>
        )}
      </div>
    </div>
  );
}
