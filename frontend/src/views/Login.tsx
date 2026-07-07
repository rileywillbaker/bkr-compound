import { FormEvent, useState } from "react";
import { api, DISCLAIMER } from "../lib/api";
import { Button, inputClass } from "../components/ui";

export default function Login({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.post("/api/auth/login", { password });
      onLogin();
    } catch {
      setError("Invalid password.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-950 p-6 text-slate-100">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-xl border border-slate-800 bg-slate-900/60 p-6">
        <h1 className="text-2xl font-bold">B-Quant</h1>
        <p className="text-sm text-slate-400">Enter the app password (APP_PASSWORD in your .env).</p>
        <input
          className={inputClass}
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Password"
          autoFocus
        />
        {error && <p className="text-sm text-rose-400">{error}</p>}
        <Button type="submit" disabled={busy || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </Button>
        <p className="text-[10px] text-slate-600">{DISCLAIMER}</p>
      </form>
    </div>
  );
}
