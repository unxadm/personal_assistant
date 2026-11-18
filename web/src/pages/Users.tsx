import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, type UserInfo } from "../api";

type Confirm = {
  user: UserInfo;
  kind: "role" | "disable" | "reset-password" | "reset-two-step" | "delete";
};

const WHAT: Record<Confirm["kind"], string> = {
  role: "change the role of",
  disable: "disable",
  "reset-password": "reset the password of",
  "reset-two-step": "turn off two-step sign-in for",
  delete: "delete",
};

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "never";
}

export default function Users({ me }: { me: number }) {
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [error, setError] = useState("");
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [role, setRole] = useState("member");
  const [password, setPassword] = useState("");
  // Shown once, because the server keeps only its hash.
  const [secret, setSecret] = useState<{ who: string; password: string } | null>(null);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [mine, setMine] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    () =>
      api
        .users()
        .then(setUsers)
        .catch((e: Error) => setError(e.message)),
    [],
  );
  useEffect(() => {
    load();
  }, [load]);

  async function run(action: () => Promise<void>) {
    setError("");
    setBusy(true);
    try {
      await action();
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const create = (e: FormEvent) => {
    e.preventDefault();
    return run(async () => {
      const made = await api.createUser({ username, display_name: displayName, role, password });
      setSecret(made.temporary_password ? { who: made.user.username, password: made.temporary_password } : null);
      setUsername("");
      setDisplayName("");
      setPassword("");
      setRole("member");
    });
  };

  const perform = () =>
    run(async () => {
      if (!confirm) return;
      const { user, kind } = confirm;
      if (kind === "role") await api.updateUser(user.id, { role: user.role === "admin" ? "member" : "admin", password: mine });
      if (kind === "disable") await api.updateUser(user.id, { disabled: !user.disabled, password: mine });
      if (kind === "reset-password") {
        const r = await api.resetUserPassword(user.id, mine);
        setSecret({ who: user.username, password: r.temporary_password });
      }
      if (kind === "reset-two-step") await api.resetUserTwoStep(user.id, mine);
      if (kind === "delete") await api.deleteUser(user.id, mine);
      setConfirm(null);
      setMine("");
    });

  const ask = (user: UserInfo, kind: Confirm["kind"]) => {
    setError("");
    setMine("");
    setConfirm({ user, kind });
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl space-y-4 p-4 pb-24">
        <div>
          <h1 className="text-lg font-semibold">People</h1>
          <p className="text-sm text-slate-400">
            Each person has their own conversations, memory, reminders, accounts and settings. As an
            admin you manage who can sign in; you cannot read what is in anyone else&apos;s account.
          </p>
        </div>

        {error && <p className="text-sm text-red-400">{error}</p>}
        {secret && (
          <div className="card space-y-2 border-amber-600">
            <p className="text-sm">
              Password for <b>{secret.who}</b> — shown once. Give it to them; they will be asked to choose
              their own when they sign in.
            </p>
            <code className="block select-all rounded bg-slate-900 p-2 text-sm">{secret.password}</code>
            <button className="btn-ghost" onClick={() => setSecret(null)}>
              I have copied it
            </button>
          </div>
        )}

        <section className="card space-y-2">
          {users.map((u) => (
            <div key={u.id} className="flex flex-wrap items-center gap-2 border-b border-slate-800 pb-2 last:border-0 last:pb-0">
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm">
                  {u.display_name || u.username}{" "}
                  <span className="text-slate-500">@{u.username}</span>
                  {u.owner && <span className="ml-2 rounded bg-slate-800 px-1.5 py-0.5 text-xs">owner</span>}
                  {u.role === "admin" && <span className="ml-2 rounded bg-sky-900 px-1.5 py-0.5 text-xs">admin</span>}
                  {u.disabled && <span className="ml-2 rounded bg-red-900 px-1.5 py-0.5 text-xs">disabled</span>}
                  {u.must_change_password && (
                    <span className="ml-2 rounded bg-amber-900 px-1.5 py-0.5 text-xs">must set password</span>
                  )}
                </div>
                <div className="text-xs text-slate-500">
                  Last sign-in {when(u.last_login_at)} · {u.sessions} device{u.sessions === 1 ? "" : "s"}
                </div>
              </div>
              {u.id !== me && (
                <div className="flex flex-wrap gap-1">
                  <button className="btn-ghost" disabled={busy} onClick={() => ask(u, "role")}>
                    {u.role === "admin" ? "Make member" : "Make admin"}
                  </button>
                  <button className="btn-ghost" disabled={busy} onClick={() => ask(u, "disable")}>
                    {u.disabled ? "Enable" : "Disable"}
                  </button>
                  <button className="btn-ghost" disabled={busy} onClick={() => ask(u, "reset-password")}>
                    Reset password
                  </button>
                  <button className="btn-ghost" disabled={busy} onClick={() => ask(u, "reset-two-step")}>
                    Reset 2-step
                  </button>
                  {!u.owner && (
                    <button className="btn-ghost text-red-400" disabled={busy} onClick={() => ask(u, "delete")}>
                      Delete
                    </button>
                  )}
                </div>
              )}
            </div>
          ))}
        </section>

        {confirm && (
          <section className="card space-y-2 border-sky-700">
            <p className="text-sm">
              To {WHAT[confirm.kind]} <b>{confirm.user.username}</b>
              {confirm.kind === "delete" && " and everything they own (this cannot be undone)"}, confirm with your own
              password.
            </p>
            <div className="flex gap-2">
              <input
                className="input"
                type="password"
                autoComplete="current-password"
                placeholder="Your password"
                autoFocus
                value={mine}
                onChange={(e) => setMine(e.target.value)}
              />
              <button className="btn" disabled={busy || !mine} onClick={perform}>
                Confirm
              </button>
              <button className="btn-ghost" onClick={() => setConfirm(null)}>
                Cancel
              </button>
            </div>
          </section>
        )}

        <form onSubmit={create} className="card space-y-3">
          <h2 className="font-medium">Add a person</h2>
          <div className="grid gap-3 sm:grid-cols-2">
            <input
              className="input"
              placeholder="Username (letters, digits, . _ -)"
              autoCapitalize="none"
              spellCheck={false}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
            <input className="input" placeholder="Name (optional)" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
            <select className="input" value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="member">Member: their own assistant only</option>
              <option value="admin">Admin: can also manage people and server settings</option>
            </select>
            <input
              className="input"
              placeholder="Password (leave empty to generate one)"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <button className="btn" disabled={busy || !username.trim()}>
            Create account
          </button>
        </form>
      </div>
    </div>
  );
}
