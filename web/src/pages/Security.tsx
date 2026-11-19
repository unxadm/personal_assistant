import QRCode from "qrcode";
import { useCallback, useEffect, useState } from "react";
import { api, type SecurityInfo } from "../api";

type Info = SecurityInfo;

export default function Security() {
  const [info, setInfo] = useState<Info | null>(null);
  const [enrol, setEnrol] = useState<{ secret: string; qr: string } | null>(null);
  const [code, setCode] = useState("");
  const [recovery, setRecovery] = useState<string[] | null>(null);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => api.security().then(setInfo).catch(() => {}), []);
  useEffect(() => {
    load();
  }, [load]);

  async function run(action: () => Promise<void>) {
    setError("");
    setNote("");
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

  const start = () =>
    run(async () => {
      const { secret, uri } = await api.totpSetup();
      setEnrol({ secret, qr: await QRCode.toDataURL(uri, { margin: 1, width: 200 }) });
    });
  const finish = () =>
    run(async () => {
      setRecovery((await api.totpEnable(code)).recovery_codes);
      setEnrol(null);
      setCode("");
    });
  // Turning it off and replacing the codes ask for the password again.
  const withPassword = (what: string, action: (password: string) => Promise<void>) => {
    if (!current) {
      setError(`Type your current password in the box below to ${what}.`);
      return;
    }
    run(() => action(current));
  };

  if (!info) return null;
  return (
    <section className="card space-y-4">
      <div>
        <h2 className="font-medium">Sign-in security</h2>
        <p className="text-xs text-slate-500">
          {info.sessions} device{info.sessions === 1 ? " is" : "s are"} signed in.
        </p>
      </div>

      <div className="space-y-2">
        <h3 className="text-sm font-medium">Two-step sign-in</h3>
        {info.totp_enabled ? (
          <>
            <p className="text-sm text-slate-300">
              On. Signing in needs a code from your authenticator app. {info.recovery_codes_left} recovery code
              {info.recovery_codes_left === 1 ? "" : "s"} left.
            </p>
            <div className="flex flex-wrap gap-2">
              <button
                className="btn-ghost"
                disabled={busy}
                onClick={() =>
                  withPassword("get new recovery codes", async (p) => {
                    setRecovery((await api.newRecoveryCodes(p)).recovery_codes);
                  })
                }
              >
                New recovery codes
              </button>
              <button
                className="btn-ghost"
                disabled={busy}
                onClick={() =>
                  withPassword("turn this off", async (p) => {
                    await api.totpDisable(p);
                    setRecovery(null);
                    setNote("Two-step sign-in is off.");
                  })
                }
              >
                Turn off
              </button>
            </div>
          </>
        ) : enrol ? (
          <div className="space-y-2">
            <p className="text-sm text-slate-300">
              Scan this with an authenticator app, or type the key in by hand, then enter the six digits it shows.
            </p>
            <img src={enrol.qr} alt="QR code for the authenticator app" width={200} height={200} className="rounded bg-white" />
            <p className="break-all font-mono text-xs text-slate-400">{enrol.secret}</p>
            <div className="flex gap-2">
              <input
                className="input w-40"
                placeholder="123456"
                inputMode="numeric"
                autoComplete="one-time-code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
              />
              <button className="btn" disabled={busy || code.trim().length < 6} onClick={finish}>
                Turn on
              </button>
              <button className="btn-ghost" onClick={() => setEnrol(null)}>
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <>
            <p className="text-sm text-slate-400">
              Off. With it on, your password alone is not enough to sign in.
            </p>
            <button className="btn-ghost" disabled={busy} onClick={start}>
              Set up
            </button>
          </>
        )}
        {recovery && (
          <div className="rounded-lg border border-amber-600/50 p-3">
            <p className="text-sm text-amber-200">
              Recovery codes. Each works once if you lose your phone. They are shown only now: save them somewhere
              safe.
            </p>
            <p className="mt-2 grid grid-cols-2 gap-1 font-mono text-sm">
              {recovery.map((c) => (
                <span key={c}>{c}</span>
              ))}
            </p>
          </div>
        )}
      </div>

      {info.sso.enabled && (
        <div className="space-y-2 border-t border-slate-800 pt-3">
          <h3 className="text-sm font-medium">{info.sso.label}</h3>
          {info.sso.linked.length > 0 ? (
            <>
              <p className="text-sm text-slate-300">
                Linked{info.sso.linked[0].email ? ` to ${info.sso.linked[0].email}` : ""}. You can sign in with{" "}
                {info.sso.label} on the sign-in page.
              </p>
              <button
                className="btn-ghost"
                disabled={busy}
                onClick={() =>
                  withPassword("unlink this", async (p) => {
                    await api.oidcUnlink(p);
                    setNote("Unlinked.");
                  })
                }
              >
                Unlink
              </button>
              {!info.has_password && (
                <p className="text-xs text-slate-500">Set a password below first if you want to be able to unlink.</p>
              )}
            </>
          ) : (
            <>
              <p className="text-sm text-slate-400">
                Not linked. Link this account to your {info.sso.label} identity to sign in with it. You will be sent
                to the provider and brought back here.
              </p>
              <button
                className="btn-ghost"
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    location.assign((await api.oidcLink()).url);
                  })
                }
              >
                Link {info.sso.label}
              </button>
            </>
          )}
        </div>
      )}

      <div className="space-y-2 border-t border-slate-800 pt-3">
        <h3 className="text-sm font-medium">{info.has_password ? "Password" : "Set a password"}</h3>
        <div className="grid gap-3 sm:grid-cols-3">
          {info.has_password && (
            <input
              className="input"
              type="password"
              placeholder="Current password"
              autoComplete="current-password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
            />
          )}
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
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            className="btn-ghost"
            disabled={busy || (info.has_password && !current) || !next || next !== confirm}
            onClick={() =>
              run(async () => {
                await api.changePassword(current, next);
                setCurrent("");
                setNext("");
                setConfirm("");
                setNote("Password changed. Other devices were signed out.");
              })
            }
          >
            {info.has_password ? "Change password" : "Set password"}
          </button>
          <button
            className="btn-ghost"
            disabled={busy || info.sessions < 2}
            onClick={() =>
              run(async () => {
                await api.revokeOtherSessions();
                setNote("Other devices were signed out.");
              })
            }
          >
            Sign out other devices
          </button>
        </div>
        {next && confirm && next !== confirm && <p className="text-xs text-red-400">The new passwords do not match.</p>}
      </div>
      {error && <p className="text-sm text-red-400">{error}</p>}
      {note && <p className="text-sm text-slate-300">{note}</p>}
    </section>
  );
}
