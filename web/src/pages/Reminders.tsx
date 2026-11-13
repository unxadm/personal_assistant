import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, type Briefing, type NotificationInfo, type Reminder } from "../api";

const REPEATS: { label: string; rrule: string }[] = [
  { label: "Does not repeat", rrule: "" },
  { label: "Every day", rrule: "FREQ=DAILY" },
  { label: "Every weekday", rrule: "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR" },
  { label: "Every week", rrule: "FREQ=WEEKLY" },
  { label: "Every month", rrule: "FREQ=MONTHLY" },
  { label: "Every year", rrule: "FREQ=YEARLY" },
];

// Times are shown in the timezone the assistant is configured for (the one "at 9" means),
// which can differ from this device's while travelling.
let zone: string | undefined;

function when(iso: string): string {
  const date = new Date(iso);
  const dayKey = (d: Date) => d.toLocaleDateString("en-CA", { timeZone: zone });
  const time = date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", timeZone: zone });
  if (dayKey(date) === dayKey(new Date())) return `Today ${time}`;
  const day = date.toLocaleDateString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: dayKey(date).slice(0, 4) === dayKey(new Date()).slice(0, 4) ? undefined : "numeric",
    timeZone: zone,
  });
  return `${day} ${time}`;
}

interface Draft {
  id: number | null; // null = new reminder
  text: string;
  at: string;
  rrule: string;
}

export default function Reminders() {
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [log, setLog] = useState<NotificationInfo[]>([]);
  const [briefing, setBriefing] = useState<Briefing["latest"]>(null);
  const [quick, setQuick] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      if (!zone) zone = (await api.settings()).timezone;
      const [r, n, b] = await Promise.all([api.reminders(), api.notifications(), api.briefing()]);
      setReminders(r);
      setLog(n);
      setBriefing(b.latest);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 30_000);
    return () => clearInterval(timer);
  }, [load]);

  async function run(label: string, action: () => Promise<unknown>) {
    setError("");
    setBusy(label);
    try {
      await action();
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }

  function addQuick(e: FormEvent) {
    e.preventDefault();
    if (!quick.trim()) return;
    run("quick", async () => {
      await api.quickReminder(quick);
      setQuick("");
    });
  }

  function saveDraft(e: FormEvent) {
    e.preventDefault();
    if (!draft || !draft.text.trim() || !draft.at) return;
    run("draft", async () => {
      if (draft.id === null) {
        await api.addReminder({ text: draft.text, at: draft.at, rrule: draft.rrule || null });
      } else {
        await api.updateReminder(draft.id, { text: draft.text, at: draft.at, rrule: draft.rrule });
      }
      setDraft(null);
    });
  }

  const waiting = reminders.filter((r) => r.status === "fired");
  const upcoming = reminders.filter((r) => r.status === "scheduled");

  function row(r: Reminder) {
    return (
      <li key={r.id} className="card flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="min-w-0 flex-1 basis-56">
          <p className="break-words">{r.text}</p>
          <p className="text-xs text-slate-400">
            {when(r.due_at)}
            {r.repeat && ` · ${r.repeat}`}
            {r.snoozed_until && ` · snoozed until ${when(r.snoozed_until)}`}
            {r.urgency === "high" && " · ignores quiet hours"}
          </p>
          {r.source === "external" && (
            <p className="text-xs text-amber-400">
              Added by the assistant while it was reading email or other outside content.
            </p>
          )}
        </div>
        <div className="flex shrink-0 gap-2">
          {(r.status === "fired" || r.snoozed_until) && (
            <button className="btn" onClick={() => run("", () => api.completeReminder(r.id))}>
              Done
            </button>
          )}
          {r.status === "fired" && (
            <button className="btn-ghost" onClick={() => run("", () => api.snoozeReminder(r.id))}>
              Snooze
            </button>
          )}
          <button
            className="btn-ghost"
            onClick={() =>
              setDraft({
                id: r.id,
                text: r.text,
                at: r.due_local,
                rrule: r.rrule ?? "",
              })
            }
          >
            Edit
          </button>
          <button
            className="btn-ghost text-red-300"
            onClick={() => confirm(`Delete “${r.text}”?`) && run("", () => api.deleteReminder(r.id))}
          >
            Delete
          </button>
        </div>
      </li>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl space-y-4 p-4">
        <div>
          <h1 className="text-lg font-semibold">Reminders</h1>
          <p className="text-sm text-slate-400">
            You can also just ask in chat: “remind me every Monday at 9 to send the report”.
          </p>
        </div>

        <form onSubmit={addQuick} className="card flex flex-col gap-2 sm:flex-row">
          <input
            className="input sm:flex-1"
            placeholder="Remind me to… e.g. call the dentist tomorrow at 9"
            value={quick}
            onChange={(e) => setQuick(e.target.value)}
          />
          <button className="btn" disabled={!quick.trim() || busy === "quick"}>
            {busy === "quick" ? "Adding…" : "Add"}
          </button>
          <button
            type="button"
            className="btn-ghost"
            onClick={() => setDraft({ id: null, text: quick, at: "", rrule: "" })}
          >
            Pick a time
          </button>
        </form>

        {draft && (
          <form onSubmit={saveDraft} className="card space-y-3">
            <h2 className="font-medium">{draft.id === null ? "New reminder" : "Edit reminder"}</h2>
            <input
              className="input"
              placeholder="What to remind you of"
              autoFocus
              value={draft.text}
              onChange={(e) => setDraft({ ...draft, text: e.target.value })}
            />
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block text-xs text-slate-400">
                When
                <input
                  className="input mt-1"
                  type="datetime-local"
                  value={draft.at}
                  onChange={(e) => setDraft({ ...draft, at: e.target.value })}
                />
              </label>
              <label className="block text-xs text-slate-400">
                Repeat
                <select
                  className="input mt-1"
                  value={draft.rrule}
                  onChange={(e) => setDraft({ ...draft, rrule: e.target.value })}
                >
                  {REPEATS.map((x) => (
                    <option key={x.rrule} value={x.rrule}>
                      {x.label}
                    </option>
                  ))}
                  {!REPEATS.some((x) => x.rrule === draft.rrule) && (
                    <option value={draft.rrule}>Custom ({draft.rrule})</option>
                  )}
                </select>
              </label>
            </div>
            <div className="flex gap-2">
              <button className="btn" disabled={!draft.text.trim() || !draft.at || busy === "draft"}>
                Save
              </button>
              <button type="button" className="btn-ghost" onClick={() => setDraft(null)}>
                Cancel
              </button>
            </div>
          </form>
        )}

        {error && <p className="text-sm text-red-400">{error}</p>}

        {waiting.length > 0 && (
          <section className="space-y-2">
            <h2 className="text-sm font-medium text-amber-300">Went off — done or snooze?</h2>
            <ul className="space-y-2">{waiting.map(row)}</ul>
          </section>
        )}

        <section className="space-y-2">
          <h2 className="text-sm font-medium text-slate-300">Upcoming</h2>
          {upcoming.length === 0 && <p className="text-sm text-slate-500">No reminders set.</p>}
          <ul className="space-y-2">{upcoming.map(row)}</ul>
        </section>

        <section className="card space-y-2">
          <div className="flex items-center justify-between gap-3">
            <h2 className="font-medium">Daily briefing</h2>
            <button
              className="btn-ghost"
              disabled={busy === "briefing"}
              onClick={() => run("briefing", api.runBriefing)}
            >
              {busy === "briefing" ? "Writing…" : "Send one now"}
            </button>
          </div>
          {briefing ? (
            <>
              <p className="whitespace-pre-wrap text-sm text-slate-200">{briefing.text}</p>
              <p className="text-xs text-slate-500">Written {when(briefing.generated_at)}</p>
            </>
          ) : (
            <p className="text-sm text-slate-500">
              None yet. Turn on the morning briefing under Settings → Notifications.
            </p>
          )}
        </section>

        {log.length > 0 && (
          <details className="card">
            <summary className="cursor-pointer text-sm font-medium">Recent notifications</summary>
            <ul className="mt-3 space-y-2 text-sm">
              {log.map((n) => {
                const sent = Object.entries(n.channels).filter(([, v]) => v === "ok").map(([k]) => k);
                const failed = Object.entries(n.channels).filter(([, v]) => v !== "ok");
                return (
                  <li key={n.id} className="border-t border-slate-800 pt-2">
                    <p className="break-words">{n.title}</p>
                    <p className="text-xs text-slate-400">
                      {when(n.created_at)} ·{" "}
                      {n.status === "sent" && `sent via ${sent.join(", ")}`}
                      {n.status === "held" && n.deliver_after && `waiting until ${when(n.deliver_after)}`}
                      {n.status === "failed" &&
                        (failed.length ? "could not be delivered" : "no notification channel is set up")}
                    </p>
                    {failed.map(([name, detail]) => (
                      <p key={name} className="text-xs text-red-400">
                        {name}: {detail}
                      </p>
                    ))}
                  </li>
                );
              })}
            </ul>
          </details>
        )}
      </div>
    </div>
  );
}
