import { useCallback, useEffect, useState } from "react";
import { api, type Learning, type Suggestion } from "../api";
import { loadZone, stamp } from "../time";

const CATEGORY: Record<string, string> = {
  bill: "Bill",
  deadline: "Deadline",
  appointment: "Appointment",
  commitment: "To do",
  travel: "Travel",
  package: "Package",
  needs_reply: "Needs a reply",
  calendar_conflict: "Calendar conflict",
  back_to_back: "No travel time",
  early_meeting: "Early start",
  meeting_brief: "Meeting brief",
};
const OUTCOME: Record<string, string> = {
  accepted: "Accepted",
  auto_applied: "Done automatically",
  dismissed: "Dismissed",
  expired: "No longer relevant",
};

export default function Inbox({
  onChanged,
  onDraft,
}: {
  onChanged: () => void;
  onDraft: (prompt: string) => void;
}) {
  const [pending, setPending] = useState<Suggestion[]>([]);
  const [history, setHistory] = useState<Suggestion[] | null>(null);
  const [learning, setLearning] = useState<Learning | null>(null);
  const [enabled, setEnabled] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    try {
      await loadZone();
      const list = await api.suggestions();
      setPending(list.suggestions);
      setEnabled(list.enabled);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    }
  }, [onChanged]);

  const loadHistory = useCallback(async () => {
    const [h, l] = await Promise.all([api.suggestions(true), api.learning()]);
    setHistory(h.suggestions);
    setLearning(l);
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 30_000);
    return () => clearInterval(timer);
  }, [load]);

  async function run(label: string, action: () => Promise<unknown>) {
    setError("");
    setNote("");
    setBusy(label);
    try {
      await action();
      await load();
      if (history) await loadHistory();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }

  function accept(s: Suggestion, mode?: "reminder") {
    run(`accept${s.id}`, async () => {
      const done = await api.acceptSuggestion(s.id, mode);
      if (s.proposed?.type === "chat" && s.proposed.prompt) onDraft(s.proposed.prompt);
      else setNote(done.result ?? "Done.");
    });
  }

  function feedbackButtons(s: Suggestion) {
    const set = (useful: boolean) =>
      run(`fb${s.id}`, () => api.suggestionFeedback(s.id, s.feedback === (useful ? "useful" : "not_useful") ? null : useful));
    const cls = (on: boolean) =>
      `rounded-lg border px-2 py-1 text-xs ${on ? "border-sky-500 text-sky-300" : "border-slate-700 text-slate-400 hover:text-slate-200"}`;
    return (
      <span className="flex gap-1">
        <button className={cls(s.feedback === "useful")} onClick={() => set(true)}>
          Useful
        </button>
        <button className={cls(s.feedback === "not_useful")} onClick={() => set(false)}>
          Not useful
        </button>
      </span>
    );
  }

  function card(s: Suggestion) {
    const isPending = s.status === "pending";
    return (
      <li key={s.id} className="card space-y-2">
        <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
          <span className="rounded bg-slate-800 px-2 py-0.5 text-slate-200">{CATEGORY[s.category] ?? s.category}</span>
          <span>{Math.round(s.confidence * 100)}% confident</span>
          <span>{stamp(s.created_at)}</span>
          {!isPending && <span className="text-slate-300">{OUTCOME[s.status]}</span>}
        </div>
        <p className="break-words font-medium">{s.title}</p>
        {s.detail && <p className="whitespace-pre-wrap break-words text-sm text-slate-300">{s.detail}</p>}
        {s.result && <p className="text-sm text-emerald-300">{s.result}</p>}
        <details className="text-xs text-slate-400">
          <summary className="cursor-pointer">Why am I seeing this?</summary>
          <p className="mt-1 break-words">
            {s.reason}{" "}
            {s.source === "llm"
              ? "Found by the model reading the email; the email's own wording is not trusted."
              : "Found by a fixed rule, without the model."}
          </p>
        </details>
        <div className="flex flex-wrap items-center gap-2">
          {isPending && s.action_label && (
            <button className="btn" disabled={busy !== ""} onClick={() => accept(s)}>
              {s.action_label}
            </button>
          )}
          {isPending && s.proposed?.type === "event" && (
            <button className="btn-ghost" disabled={busy !== ""} onClick={() => accept(s, "reminder")}>
              Remind me instead
            </button>
          )}
          {isPending && (
            <button
              className="btn-ghost"
              disabled={busy !== ""}
              onClick={() => run(`dismiss${s.id}`, () => api.dismissSuggestion(s.id))}
            >
              {s.action_label ? "Dismiss" : "Got it"}
            </button>
          )}
          {s.url && (
            <a className="text-xs text-sky-400 hover:underline" href={s.url} target="_blank" rel="noreferrer">
              Open source
            </a>
          )}
          <span className="ml-auto">{feedbackButtons(s)}</span>
        </div>
      </li>
    );
  }

  const tuned = learning ? Object.entries(learning.categories).filter(([, c]) => c.bias !== 0) : [];

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl space-y-4 p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h1 className="font-medium">Suggestions</h1>
            <p className="text-xs text-slate-500">
              Things noticed in your mail and calendar. Nothing here happens until you say so.
            </p>
          </div>
          <button
            className="btn-ghost shrink-0"
            disabled={busy !== "" || !enabled}
            onClick={() =>
              run("check", async () => {
                const r = await api.checkNow();
                setNote(r.created ? `Found ${r.created} new.` : "Nothing new.");
              })
            }
          >
            {busy === "check" ? "Checking…" : "Check now"}
          </button>
        </div>
        {!enabled && (
          <p className="text-sm text-amber-300">Proactive suggestions are turned off in Settings.</p>
        )}
        {error && <p className="text-sm text-red-400">{error}</p>}
        {note && <p className="text-sm text-slate-300">{note}</p>}

        {pending.length === 0 ? (
          <p className="text-sm text-slate-500">Nothing waiting for you.</p>
        ) : (
          <ul className="space-y-3">{pending.map(card)}</ul>
        )}

        <details
          className="card"
          onToggle={(e) => {
            if ((e.target as HTMLDetailsElement).open && !history) loadHistory().catch(() => {});
          }}
        >
          <summary className="cursor-pointer text-sm font-medium">Earlier suggestions and what feedback changed</summary>
          <div className="mt-3 space-y-3">
            {learning && (
              <div className="text-xs text-slate-400">
                {tuned.length === 0 && learning.muted_senders.length === 0 && (
                  <p>No adjustments yet. “Useful” and “Not useful” raise or lower how readily a kind of suggestion is shown.</p>
                )}
                {tuned.map(([name, c]) => (
                  <p key={name}>
                    {CATEGORY[name] ?? name}: shown {c.bias > 0 ? "more" : "less"} readily (
                    {c.useful} useful, {c.not_useful} not useful)
                  </p>
                ))}
                {learning.muted_senders.length > 0 && (
                  <p>No longer analysing mail from: {learning.muted_senders.join(", ")}</p>
                )}
              </div>
            )}
            {history && history.length === 0 && <p className="text-sm text-slate-500">None yet.</p>}
            <ul className="space-y-3">{history?.map(card)}</ul>
          </div>
        </details>
      </div>
    </div>
  );
}
