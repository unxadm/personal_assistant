import { useState, type FormEvent } from "react";
import { api } from "../api";

export default function Login({
  setup,
  needsSetupCode,
  onDone,
}: {
  setup: boolean;
  needsSetupCode: boolean;
  onDone: () => void;
}) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [setupCode, setSetupCode] = useState("");
  // Two-step sign-in: asked for only after the password was accepted.
  const [askCode, setAskCode] = useState(false);
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (setup && password !== confirm) {
      setError("Passwords do not match");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await (setup ? api.setup(password, setupCode) : api.login(password, code));
      onDone();
    } catch (err) {
      const message = (err as Error).message;
      if (message === "code_required") {
        setAskCode(true);
      } else {
        setError(message);
        setCode("");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full items-center justify-center p-4">
      <form onSubmit={submit} className="card w-full max-w-sm space-y-3">
        <h1 className="text-lg font-semibold">{setup ? "Set a password" : "Sign in"}</h1>
        {setup && (
          <p className="text-sm text-slate-400">
            First run. Choose the password that protects this assistant (10+ characters).
          </p>
        )}
        {!askCode && (
          <input
            className="input"
            type="password"
            placeholder="Password"
            autoFocus
            autoComplete={setup ? "new-password" : "current-password"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        )}
        {setup && (
          <input
            className="input"
            type="password"
            placeholder="Repeat password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
          />
        )}
        {setup && needsSetupCode && (
          <label className="block text-xs text-slate-400">
            Setup code, printed in the server log when it started
            <input
              className="input mt-1"
              autoComplete="off"
              value={setupCode}
              onChange={(e) => setSetupCode(e.target.value)}
            />
          </label>
        )}
        {askCode && (
          <label className="block text-xs text-slate-400">
            Code from your authenticator app, or a recovery code
            <input
              className="input mt-1"
              autoFocus
              autoComplete="one-time-code"
              inputMode="text"
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
          </label>
        )}
        {error && <p className="text-sm text-red-400">{error}</p>}
        <button className="btn w-full" disabled={busy || !password || (askCode && !code)}>
          {setup ? "Save and continue" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
