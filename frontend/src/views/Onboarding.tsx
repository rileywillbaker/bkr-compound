// First-run wizard: starting equity + watchlist → API keys (paste + test) →
// finish. Keys can also be managed later in Settings → Providers.

import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, DISCLAIMER } from "../lib/api";
import ProviderKeys from "../components/ProviderKeys";
import { Button, Card, Field, inputClass } from "../components/ui";

export default function Onboarding({ onDone }: { onDone: () => void }) {
  const [step, setStep] = useState(0);
  const [equity, setEquity] = useState("10000");
  const [watchlist, setWatchlist] = useState("SPY, QQQ, NVDA, AAPL, MSFT");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  const saveBasics = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const value = parseFloat(equity);
      if (!Number.isFinite(value) || value <= 0) throw new Error("Starting equity must be a positive number.");
      await api.put("/api/settings/equity", { starting_equity: value });
      const symbols = watchlist.split(",").map((s) => s.trim()).filter(Boolean);
      if (symbols.length === 0) throw new Error("Add at least one ticker.");
      await api.put("/api/settings/watchlist", { symbols });
      setStep(1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "save failed");
    } finally {
      setBusy(false);
    }
  };

  const finish = async () => {
    setBusy(true);
    try {
      await api.post("/api/settings/onboarding-complete");
      onDone();
      navigate("/");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 p-6 text-slate-100">
      <div className="mx-auto max-w-3xl space-y-6">
        <header>
          <h1 className="text-3xl font-bold">Welcome to B-Quant</h1>
          <p className="mt-1 text-slate-400">
            Your AI stock-analysis copilot. It researches, scores, and alerts — <strong>you</strong> place every trade.
          </p>
          <p className="mt-2 text-xs text-slate-500">{DISCLAIMER}</p>
        </header>

        <ol className="flex gap-2 text-xs">
          {["Account basics", "API keys", "Finish"].map((label, i) => (
            <li
              key={label}
              className={`rounded-full px-3 py-1 ${
                i === step ? "bg-sky-500/20 text-sky-300" : i < step ? "bg-emerald-500/15 text-emerald-400" : "bg-slate-800 text-slate-500"
              }`}
            >
              {i + 1}. {label}
            </li>
          ))}
        </ol>

        {step === 0 && (
          <Card title="Step 1 — Account basics">
            <form onSubmit={saveBasics} className="space-y-4">
              <Field label="Starting equity (USD) — used for position sizing and risk limits">
                <input className={inputClass} value={equity} onChange={(e) => setEquity(e.target.value)} inputMode="decimal" />
              </Field>
              <Field label="Watchlist — tickers the pipeline scans, comma-separated">
                <input className={inputClass} value={watchlist} onChange={(e) => setWatchlist(e.target.value)} />
              </Field>
              {error && <p className="text-sm text-rose-400">{error}</p>}
              <Button type="submit" disabled={busy}>
                {busy ? "Saving…" : "Continue"}
              </Button>
            </form>
          </Card>
        )}

        {step === 1 && (
          <div className="space-y-4">
            <Card title="Step 2 — Connect data providers">
              <p className="mb-3 text-sm text-slate-400">
                Each card below has step-by-step instructions to get a free API key. Paste the key, hit{" "}
                <em>Save</em>, then <em>Test connection</em>. Keys are encrypted on the server and never shown again.
                You can skip any of these and finish them later in Settings.
              </p>
              <ProviderKeys />
            </Card>
            <div className="flex gap-2">
              <Button variant="ghost" onClick={() => setStep(0)}>Back</Button>
              <Button onClick={() => setStep(2)}>Continue</Button>
            </div>
          </div>
        )}

        {step === 2 && (
          <Card title="Step 3 — You're set">
            <ul className="mb-4 list-disc space-y-1 pl-5 text-sm text-slate-300">
              <li>The scheduler ingests data and scans your watchlist during market hours.</li>
              <li>High-confidence, risk-approved BUY/SELL ideas arrive via Telegram (if configured) and the Signals feed.</li>
              <li>Most days the right output is <strong>no trade</strong> — silence is a feature, not a bug.</li>
              <li>Tune risk limits, watchlist, and alert thresholds anytime in Settings.</li>
            </ul>
            <div className="flex gap-2">
              <Button variant="ghost" onClick={() => setStep(1)}>Back</Button>
              <Button onClick={finish} disabled={busy}>{busy ? "Finishing…" : "Enter B-Quant"}</Button>
            </div>
          </Card>
        )}
      </div>
    </div>
  );
}
