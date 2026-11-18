import { useState, type FormEvent } from "react";
import { api } from "../api";

/** Shown instead of the app after an admin created the account or reset its password. */
export default function ChangePassword({ username, onDone }: { username: string; onDone: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (next !== confirm) {
      setError("Passwords do not match");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api.changePassword(current, next);
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full items-center justify-center p-4">
      <form onSubmit={submit} className="card w-full max-w-sm space-y-3">
        <h1 className="text-lg font-semibold">Choose your own password</h1>
        <p className="text-sm text-slate-400">
          {username}, an admin set the password you just used. Pick one only you know (10+
          characters) before you continue.
        </p>
        <input
          className="input"
          type="password"
          placeholder="Password you were given"
          autoComplete="current-password"
          autoFocus
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
        />
        <input
          className="input"
          type="password"
          placeholder="New password"
          autoComplete="new-password"
          value={next}
          onChange={(e) => setNext(e.target.value)}
        />
        <input
          className="input"
          type="password"
          placeholder="Repeat new password"
          autoComplete="new-password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
        />
        {error && <p className="text-sm text-red-400">{error}</p>}
        <div className="flex gap-2">
          <button className="btn flex-1" disabled={busy || !current || !next}>
            Save password
          </button>
          <button type="button" className="btn-ghost" onClick={() => api.logout().finally(onDone)}>
            Sign out
          </button>
        </div>
      </form>
    </div>
  );
}
