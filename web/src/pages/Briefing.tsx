import { useCallback, useEffect, useState } from "react";
import { api, type BriefingRecord } from "../api";
import { loadZone, stamp } from "../time";

function day(date: string): string {
  // A calendar date, not an instant: format it without shifting it across timezones.
  return new Date(`${date}T12:00:00Z`).toLocaleDateString(undefined, {
    weekday: "long",
    day: "numeric",
    month: "long",
    timeZone: "UTC",
  });
}

function Body({ briefing }: { briefing: BriefingRecord }) {
  return (
    <div className="space-y-5">
      {briefing.sections.map((section, i) => (
        <section key={i} className="space-y-1.5">
          {section.title && <h3 className="text-sm font-semibold text-sky-300">{section.title}</h3>}
          <p className="whitespace-pre-wrap text-[15px] leading-relaxed text-slate-200">{section.body}</p>
        </section>
      ))}
    </div>
  );
}

export default function Briefing({ onSettings }: { onSettings: () => void }) {
  const [latest, setLatest] = useState<BriefingRecord | null>(null);
  const [history, setHistory] = useState<BriefingRecord[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [time, setTime] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [writing, setWriting] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      await loadZone();
      const [b, settings] = await Promise.all([api.briefing(), api.settings()]);
      setLatest(b.latest);
      setHistory(b.history);
      setEnabled(settings.briefing.enabled);
      setTime(settings.briefing.time);
      setLoaded(true);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function write() {
    setWriting(true);
    setError("");
    try {
      await api.runBriefing();
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setWriting(false);
    }
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-2xl space-y-5 p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h1 className="text-lg font-semibold">{latest ? day(latest.date) : "Daily briefing"}</h1>
            {latest && <p className="text-xs text-slate-500">Written {stamp(latest.generated_at)}</p>}
          </div>
          <button className="btn-ghost shrink-0" disabled={writing} onClick={write}>
            {writing ? "Writing…" : latest ? "Write a new one" : "Write one now"}
          </button>
        </div>

        {error && <p className="text-sm text-red-400">{error}</p>}

        {loaded && !enabled && (
          <p className="card text-sm text-slate-400">
            The morning briefing is off, so nothing is written on its own.{" "}
            <button className="text-sky-400 hover:underline" onClick={onSettings}>
              Turn it on in Settings
            </button>
            .
          </p>
        )}

        {latest ? (
          <article className="card">
            <Body briefing={latest} />
          </article>
        ) : (
          loaded && (
            <p className="text-sm text-slate-500">
              No briefing yet.{enabled && time ? ` The next one is written at ${time}.` : ""}
            </p>
          )
        )}

        {history.length > 0 && (
          <section className="space-y-2">
            <h2 className="text-sm font-medium text-slate-300">Earlier</h2>
            {history.map((h) => (
              <details key={h.date} className="card">
                <summary className="cursor-pointer text-sm font-medium">{day(h.date)}</summary>
                <div className="mt-3">
                  <Body briefing={h} />
                </div>
              </details>
            ))}
          </section>
        )}
      </div>
    </div>
  );
}
