import { useEffect, useState, type FormEvent } from "react";
import { api, type Fact } from "../api";

export default function Memory() {
  const [facts, setFacts] = useState<Fact[]>([]);
  const [content, setContent] = useState("");
  const [category, setCategory] = useState("general");
  const [editing, setEditing] = useState<{ id: number; content: string } | null>(null);
  const [error, setError] = useState("");

  const load = () =>
    api
      .facts()
      .then(setFacts)
      .catch((e: Error) => setError(e.message));

  useEffect(() => {
    load();
  }, []);

  async function run(action: () => Promise<unknown>) {
    setError("");
    try {
      await action();
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  function add(e: FormEvent) {
    e.preventDefault();
    if (!content.trim()) return;
    run(async () => {
      await api.addFact(content, category || "general");
      setContent("");
    });
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl space-y-4 p-4">
        <div>
          <h1 className="text-lg font-semibold">Memory</h1>
          <p className="text-sm text-slate-400">
            Facts the assistant knows about you. They are included in every conversation. Edit or
            delete anything here.
          </p>
        </div>
        <form onSubmit={add} className="card flex flex-col gap-2 sm:flex-row">
          <input
            className="input sm:flex-1"
            placeholder="Add a fact, e.g. “I work from home on Fridays”"
            value={content}
            onChange={(e) => setContent(e.target.value)}
          />
          <input
            className="input sm:w-36"
            placeholder="Category"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
          />
          <button className="btn" disabled={!content.trim()}>
            Add
          </button>
        </form>
        {error && <p className="text-sm text-red-400">{error}</p>}
        {facts.length === 0 && <p className="text-sm text-slate-500">Nothing remembered yet.</p>}
        <ul className="space-y-2">
          {facts.map((f) => (
            <li key={f.id} className="card flex items-start gap-3">
              <div className="min-w-0 flex-1">
                {editing?.id === f.id ? (
                  <textarea
                    className="input"
                    autoFocus
                    value={editing.content}
                    onChange={(e) => setEditing({ id: f.id, content: e.target.value })}
                  />
                ) : (
                  <p className="break-words text-sm">{f.content}</p>
                )}
                <p className="mt-1 text-xs text-slate-500">
                  {f.category} · {f.source === "agent" ? "saved by assistant" : "added by you"} ·{" "}
                  {new Date(f.updated_at).toLocaleDateString()}
                </p>
              </div>
              {editing?.id === f.id ? (
                <>
                  <button
                    className="btn"
                    onClick={() =>
                      run(async () => {
                        await api.updateFact(f.id, { content: editing.content });
                        setEditing(null);
                      })
                    }
                  >
                    Save
                  </button>
                  <button className="btn-ghost" onClick={() => setEditing(null)}>
                    Cancel
                  </button>
                </>
              ) : (
                <>
                  <button className="btn-ghost" onClick={() => setEditing({ id: f.id, content: f.content })}>
                    Edit
                  </button>
                  <button className="btn-ghost" onClick={() => run(() => api.deleteFact(f.id))}>
                    Delete
                  </button>
                </>
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
