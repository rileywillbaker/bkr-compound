// Dashboard (spec §7.2): regime banner, portfolio snapshot, watchlist tiles,
// today's signals; manual scan trigger.

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, AppSettings, PortfolioValuation, SignalSummary } from "../lib/api";
import { useFeed } from "../lib/ws";
import { ActionBadge, Button, Card, ErrorNote, Spinner, fmtMoney, fmtWhen } from "../components/ui";

interface LastRun {
  run_id: string;
  regime: string | null;
  symbols: string[];
  signals: SignalSummary[];
}

const REGIME_LABELS: Record<string, string> = {
  bull_trend: "🟢 Bull trend",
  bear_trend: "🔴 Bear trend",
  range: "🟡 Range-bound",
  high_volatility: "🟠 High volatility",
};

export default function Dashboard() {
  const [portfolio, setPortfolio] = useState<PortfolioValuation | null>(null);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [lastRun, setLastRun] = useState<LastRun | null>(null);
  const [today, setToday] = useState<SignalSummary[]>([]);
  const [error, setError] = useState("");
  const [scanning, setScanning] = useState(false);

  const refresh = useCallback(() => {
    const midnight = new Date();
    midnight.setHours(0, 0, 0, 0);
    Promise.all([
      api.get<PortfolioValuation>("/api/portfolio"),
      api.get<AppSettings>("/api/settings"),
      api.get<LastRun | null>("/api/pipeline/last"),
      api.get<{ signals: SignalSummary[] }>(`/api/signals?since=${midnight.toISOString()}&limit=20`),
    ])
      .then(([p, s, run, sig]) => {
        setPortfolio(p);
        setSettings(s);
        setLastRun(run);
        setToday(sig.signals);
        setError("");
      })
      .catch((e) => setError(e instanceof Error ? e.message : "load failed"));
  }, []);

  useEffect(refresh, [refresh]);
  useFeed(() => refresh());

  const scan = async () => {
    setScanning(true);
    try {
      await api.post("/api/pipeline/run", {});
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "scan failed");
    } finally {
      setScanning(false);
    }
  };

  if (error) return <ErrorNote message={error} />;
  if (!portfolio || !settings) return <Spinner />;

  const regime = lastRun?.regime ?? null;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Dashboard</h1>
        <Button onClick={scan} disabled={scanning}>
          {scanning ? "Scanning…" : "Run scan now"}
        </Button>
      </div>

      <div className={`rounded-xl border p-4 ${regime ? "border-sky-500/30 bg-sky-500/5" : "border-slate-800 bg-slate-900/60"}`}>
        <span className="text-xs uppercase tracking-wider text-slate-500">Market regime</span>
        <p className="mt-1 text-lg font-semibold">
          {regime ? REGIME_LABELS[regime] ?? regime : "No scan yet today — run one or wait for the scheduler."}
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {[
          ["Equity", fmtMoney(portfolio.equity)],
          ["Cash", fmtMoney(portfolio.cash)],
          ["Day P&L", fmtMoney(portfolio.day_pnl)],
          ["Gross exposure", `${portfolio.gross_exposure_pct.toFixed(1)}%`],
        ].map(([label, value]) => (
          <Card key={label}>
            <span className="text-xs uppercase tracking-wider text-slate-500">{label}</span>
            <p className={`mt-1 text-xl font-semibold ${label === "Day P&L" ? (portfolio.day_pnl >= 0 ? "text-emerald-400" : "text-rose-400") : ""}`}>
              {value}
            </p>
          </Card>
        ))}
      </div>

      <Card title={`Watchlist (${settings.watchlist.length})`}>
        <div className="flex flex-wrap gap-2">
          {settings.watchlist.map((sym) => {
            const pos = portfolio.positions.find((p) => p.symbol === sym);
            return (
              <div key={sym} className="rounded-lg border border-slate-800 bg-slate-950 px-3 py-2">
                <span className="font-mono font-semibold">{sym}</span>
                {pos && (
                  <span className={`ml-2 text-xs ${pos.unrealized_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                    {pos.shares} sh · {fmtMoney(pos.unrealized_pnl)}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      </Card>

      <Card
        title="Today's signals"
        actions={<Link to="/signals" className="text-xs text-sky-400 hover:underline">Full feed →</Link>}
      >
        {today.length === 0 ? (
          <p className="text-sm text-slate-500">
            No signals yet today. NO TRADE is a first-class outcome — most days that's correct.
          </p>
        ) : (
          <ul className="divide-y divide-slate-800">
            {today.map((s) => (
              <li key={s.id} className="flex items-center gap-3 py-2 text-sm">
                <ActionBadge action={s.action} />
                <span className="font-mono font-semibold">{s.ticker}</span>
                <span className="text-slate-400">{(s.confidence * 100).toFixed(0)}% conf · {s.strategy}</span>
                <span className="ml-auto text-xs text-slate-500">{fmtWhen(s.created_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
