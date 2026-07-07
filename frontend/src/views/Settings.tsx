// Settings (spec §7.7): versioned risk-profile editor, watchlist manager,
// equity, alert quiet hours, and provider API keys. Risk limits change ONLY
// here — never automatically.

import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, AppSettings } from "../lib/api";
import ProviderKeys from "../components/ProviderKeys";
import { Button, Card, ErrorNote, Field, Spinner, inputClass } from "../components/ui";

const RISK_FIELD_LABELS: Record<string, string> = {
  risk_per_trade_pct: "Risk per trade (% of equity)",
  atr_stop_multiple: "Stop distance (× ATR-14)",
  min_reward_risk: "Minimum reward:risk",
  max_position_pct: "Max position size (% of equity)",
  max_open_positions: "Max open positions",
  max_daily_loss_pct: "Max daily loss (%) — breach halts BUYs",
  max_drawdown_pct: "Max drawdown from high-water mark (%)",
  max_sector_pct: "Max sector concentration (%)",
  max_correlated_exposure_pct: "Max correlated exposure (%)",
  correlation_threshold: "Correlation threshold (0–1)",
  min_avg_dollar_volume: "Min avg dollar volume ($/day)",
  max_adv_participation_pct: "Max % of avg daily volume",
  max_atr_pct: "Max volatility (ATR as % of price)",
  earnings_blackout_days: "Earnings blackout (trading days)",
  max_portfolio_exposure_pct: "Max gross exposure (%)",
  alert_confidence_threshold: "Alert confidence threshold (0–1)",
  max_alerts_per_day: "Max alerts per day",
};

function RiskProfileEditor() {
  const [profile, setProfile] = useState<Record<string, number> | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [versions, setVersions] = useState<{ version: number; created_at: string }[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api.get<Record<string, number>>("/api/risk/profile").then(setProfile);
    api.get<{ version: number; created_at: string }[]>("/api/risk/profile/versions").then(setVersions).catch(() => {});
  }, []);
  useEffect(load, [load]);

  if (!profile) return <Spinner />;

  const save = async () => {
    setBusy(true);
    setMessage("");
    try {
      const params: Record<string, number> = {};
      for (const [key, raw] of Object.entries(edits)) {
        if (raw.trim() === "") continue;
        const value = Number(raw);
        if (!Number.isFinite(value)) throw new Error(`${RISK_FIELD_LABELS[key] ?? key}: not a number`);
        params[key] = value;
      }
      if (Object.keys(params).length === 0) throw new Error("nothing changed");
      const updated = await api.put<Record<string, number>>("/api/risk/profile", params);
      setProfile(updated);
      setEdits({});
      setMessage(`Saved as version ${updated.version}. The risk engine applies it to the next scan.`);
      load();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "save failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title={`Risk profile (active version ${profile.version})`}>
      <p className="mb-3 text-xs text-slate-500">
        Every edit is validated and saved as a new version — full history is kept. The deterministic risk
        engine enforces these limits with absolute veto; there is no override.
      </p>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {Object.entries(RISK_FIELD_LABELS).map(([key, label]) => (
          <Field key={key} label={label}>
            <input
              className={inputClass}
              value={edits[key] ?? String(profile[key] ?? "")}
              onChange={(e) => setEdits({ ...edits, [key]: e.target.value })}
              inputMode="decimal"
            />
          </Field>
        ))}
      </div>
      <div className="mt-4 flex items-center gap-3">
        <Button onClick={save} disabled={busy || Object.keys(edits).length === 0}>
          {busy ? "Saving…" : "Save as new version"}
        </Button>
        {message && <span className="text-sm text-slate-400">{message}</span>}
      </div>
      {versions.length > 1 && (
        <p className="mt-3 text-xs text-slate-500">
          History: {versions.map((v) => `v${v.version}`).join(" · ")}
        </p>
      )}
    </Card>
  );
}

export default function Settings() {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [watchlist, setWatchlist] = useState("");
  const [equity, setEquity] = useState("");
  const [quietStart, setQuietStart] = useState("");
  const [quietEnd, setQuietEnd] = useState("");
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    api
      .get<AppSettings>("/api/settings")
      .then((s) => {
        setSettings(s);
        setWatchlist(s.watchlist.join(", "));
        setEquity(String(s.starting_equity));
        setQuietStart(s.alert_quiet_hours?.start ?? "");
        setQuietEnd(s.alert_quiet_hours?.end ?? "");
      })
      .catch((e) => setError(e instanceof Error ? e.message : "load failed"));
  }, []);

  const saveGeneral = async (e: FormEvent) => {
    e.preventDefault();
    setNote("");
    try {
      const symbols = watchlist.split(",").map((s) => s.trim()).filter(Boolean);
      await api.put("/api/settings/watchlist", { symbols });
      await api.put("/api/settings/equity", { starting_equity: parseFloat(equity) });
      await api.put("/api/settings/quiet-hours", {
        start: quietStart || null,
        end: quietEnd || null,
      });
      setNote("Saved.");
    } catch (err) {
      setNote(err instanceof Error ? err.message : "save failed");
    }
  };

  if (error) return <ErrorNote message={error} />;
  if (!settings) return <Spinner />;

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">Settings</h1>

      <Card title="General">
        <form onSubmit={saveGeneral} className="space-y-3">
          <Field label="Watchlist (comma-separated tickers the pipeline scans)">
            <input className={inputClass} value={watchlist} onChange={(e) => setWatchlist(e.target.value)} />
          </Field>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Starting equity (USD)">
              <input className={inputClass} value={equity} onChange={(e) => setEquity(e.target.value)} inputMode="decimal" />
            </Field>
            <Field label="Alert quiet hours start (ET, HH:MM — blank = off)">
              <input className={inputClass} value={quietStart} onChange={(e) => setQuietStart(e.target.value)} placeholder="22:00" />
            </Field>
            <Field label="Alert quiet hours end (ET, HH:MM)">
              <input className={inputClass} value={quietEnd} onChange={(e) => setQuietEnd(e.target.value)} placeholder="07:00" />
            </Field>
          </div>
          <div className="flex items-center gap-3">
            <Button type="submit">Save general settings</Button>
            {note && <span className="text-sm text-slate-400">{note}</span>}
          </div>
        </form>
      </Card>

      <RiskProfileEditor />

      <Card title="Data provider API keys">
        <p className="mb-3 text-xs text-slate-500">
          Keys are stored encrypted server-side and never sent back to the browser. Each card includes
          step-by-step instructions for obtaining the key.
        </p>
        <ProviderKeys />
      </Card>
    </div>
  );
}
