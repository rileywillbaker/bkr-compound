// Swing Trading (book="swing"): a SEPARATE feed of 2–10 day setups, screened
// from the S&P 500 for liquidity/volatility/structure and run through the SAME
// deterministic risk engine as the long-term book. Informational only — you
// place every trade. The long-term Signals feed is unaffected.

import { useCallback, useEffect, useState } from "react";
import { api, DISCLAIMER, SignalDetail, SignalSummary, SwingFeed } from "../lib/api";
import { useFeed } from "../lib/ws";
import { ActionBadge, Button, Card, ErrorNote, Pill, Spinner, fmtMoney, fmtWhen } from "../components/ui";

const REGIME_LABELS: Record<string, string> = {
  "bull-trend": "🟢 Bull trend",
  "bear-trend": "🔴 Bear trend",
  range: "🟡 Range-bound",
  "high-volatility": "🟠 High volatility",
};

const STRATEGY_LABELS: Record<string, string> = {
  "swing-pullback": "Pullback in uptrend",
  "swing-breakout": "Breakout continuation",
  "swing-oversold": "Oversold bounce",
  "swing-cash": "No trade (stand aside)",
};

function RiskTable({ detail }: { detail: SignalDetail }) {
  if (!detail.risk_check) return <p className="text-xs text-slate-500">No risk check (informational only).</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-slate-500">
            <th className="py-1 pr-3">Rule</th>
            <th className="py-1 pr-3">Value</th>
            <th className="py-1 pr-3">Limit</th>
            <th className="py-1 pr-3">Result</th>
            <th className="py-1">Detail</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800">
          {detail.risk_check.rules.map((r) => (
            <tr key={r.rule}>
              <td className="py-1 pr-3 font-mono">{r.rule}</td>
              <td className="py-1 pr-3">{r.value ?? "—"}</td>
              <td className="py-1 pr-3">{r.limit ?? "—"}</td>
              <td className="py-1 pr-3">
                <Pill ok={r.passed}>{r.passed ? "pass" : "FAIL"}</Pill>
              </td>
              <td className="py-1 text-slate-400">{r.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-xs">
        Overall:{" "}
        <Pill ok={detail.risk_check.approved}>
          {detail.risk_check.approved ? "APPROVED" : "VETOED"} (profile v{detail.risk_check.profile_version})
        </Pill>
      </p>
    </div>
  );
}

function SetupRow({ setup, onDecided }: { setup: SignalSummary; onDecided: () => void }) {
  const [detail, setDetail] = useState<SignalDetail | null>(null);
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const toggle = async () => {
    if (!open && !detail) setDetail(await api.get<SignalDetail>(`/api/signals/${setup.id}`));
    setOpen(!open);
  };

  const decide = async (decision: "taken" | "skipped" | "modified") => {
    setBusy(true);
    try {
      await api.post(`/api/signals/${setup.id}/decision`, { decision, note });
      onDecided();
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="py-2">
      <button onClick={toggle} className="flex w-full items-center gap-3 text-left text-sm">
        <ActionBadge action={setup.action} />
        <span className="font-mono font-semibold">{setup.ticker}</span>
        <span className="text-slate-400">
          {(setup.confidence * 100).toFixed(0)}% · risk {setup.risk_score}/10 ·{" "}
          {STRATEGY_LABELS[setup.strategy] ?? setup.strategy}
        </span>
        {setup.deterministic_only && <Pill ok={false}>deterministic-only</Pill>}
        {setup.user_decision && <Pill ok>{setup.user_decision}</Pill>}
        <span className="ml-auto text-xs text-slate-500">{fmtWhen(setup.created_at)}</span>
        <span className="text-slate-600">{open ? "▾" : "▸"}</span>
      </button>

      {open && detail && (
        <div className="mt-3 space-y-4 rounded-lg border border-slate-800 bg-slate-950 p-4">
          {(setup.action === "BUY" || setup.action === "SELL") && (
            <div className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
              <div><span className="text-xs text-slate-500">Shares</span><p className="font-semibold">{detail.shares ?? "—"}</p></div>
              <div><span className="text-xs text-slate-500">Max entry</span><p className="font-semibold">{fmtMoney(detail.max_entry_price)}</p></div>
              <div><span className="text-xs text-slate-500">Stop loss</span><p className="font-semibold text-rose-300">{fmtMoney(detail.stop_loss)}</p></div>
              <div><span className="text-xs text-slate-500">Target</span><p className="font-semibold text-emerald-300">{fmtMoney(detail.take_profit)}</p></div>
            </div>
          )}
          <p className="text-sm text-slate-300">{detail.explanation}</p>

          <div>
            <h4 className="mb-1 text-xs font-semibold uppercase tracking-wider text-slate-500">Evidence</h4>
            {detail.evidence.length === 0 ? (
              <p className="text-xs text-slate-500">none recorded</p>
            ) : (
              <ul className="space-y-1 text-xs text-slate-400">
                {detail.evidence.map((e, i) => (
                  <li key={i}>
                    <span className="font-mono text-slate-500">[{e.source}]</span> {e.datapoint}
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div>
            <h4 className="mb-1 text-xs font-semibold uppercase tracking-wider text-slate-500">Risk check</h4>
            <RiskTable detail={detail} />
          </div>

          {!setup.user_decision && (
            <div className="flex flex-wrap items-center gap-2 border-t border-slate-800 pt-3">
              <span className="text-xs text-slate-500">Your decision:</span>
              <input
                className="w-64 rounded-lg border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-100 outline-none focus:border-sky-500"
                placeholder="optional note"
                value={note}
                onChange={(e) => setNote(e.target.value)}
              />
              <Button onClick={() => decide("taken")} disabled={busy}>Taken</Button>
              <Button variant="ghost" onClick={() => decide("modified")} disabled={busy}>Modified</Button>
              <Button variant="ghost" onClick={() => decide("skipped")} disabled={busy}>Skipped</Button>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

export default function Trading() {
  const [feed, setFeed] = useState<SwingFeed | null>(null);
  const [error, setError] = useState("");
  const [scanning, setScanning] = useState(false);

  const load = useCallback(() => {
    api
      .get<SwingFeed>("/api/trading")
      .then((f) => {
        setFeed(f);
        setError("");
      })
      .catch((e) => setError(e instanceof Error ? e.message : "load failed"));
  }, []);

  useEffect(load, [load]);
  useFeed((event) => {
    if (event.kind === "swing_signal") load();
  });

  const scan = async () => {
    setScanning(true);
    try {
      await api.post("/api/trading/scan", {});
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "scan failed");
    } finally {
      setScanning(false);
    }
  };

  if (error) return <ErrorNote message={error} />;
  if (!feed) return <Spinner />;

  const setups = feed.signals.filter((s) => s.action === "BUY" || s.action === "SELL");
  const regime = feed.regime;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Swing Trading</h1>
          <p className="text-xs text-slate-500">2–10 day setups · separate from your long-term book</p>
        </div>
        <Button onClick={scan} disabled={scanning}>
          {scanning ? "Scanning…" : "Scan now"}
        </Button>
      </div>

      <div className={`rounded-xl border p-4 ${regime ? "border-sky-500/30 bg-sky-500/5" : "border-slate-800 bg-slate-900/60"}`}>
        <span className="text-xs uppercase tracking-wider text-slate-500">Market regime</span>
        <p className="mt-1 text-lg font-semibold">
          {regime ? REGIME_LABELS[regime] ?? regime : "No swing scan yet — press “Scan now” or wait for the scheduler."}
        </p>
        {feed.generated_at && (
          <p className="mt-1 text-xs text-slate-500">
            Last scan {fmtWhen(feed.generated_at)} · screened {feed.screened_count ?? 0} of {feed.scanned ?? 0} names
            {typeof feed.alerts_sent === "number" ? ` · ${feed.alerts_sent} alert${feed.alerts_sent === 1 ? "" : "s"} sent` : ""}
          </p>
        )}
      </div>

      <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 text-xs leading-relaxed text-amber-200/90">
        Swing trading is higher-risk and faster-moving than long-term investing. These are <b>ideas, not orders</b> —
        every setup lists an exact entry, stop, target and share count and has already passed the same deterministic
        risk engine as the rest of B-Quant. Nothing here is auto-loosened to create more signals, and no trade is placed
        for you. Most quiet days will show nothing, and that's fine.
      </div>

      <Card title="Today's swing setups">
        {setups.length === 0 ? (
          <p className="text-sm text-slate-500">
            No swing setups right now. NO TRADE is a first-class outcome — most days that's correct.
          </p>
        ) : (
          <ul className="divide-y divide-slate-800">
            {setups.map((s) => (
              <SetupRow key={s.id} setup={s} onDecided={load} />
            ))}
          </ul>
        )}
      </Card>

      <Card title="How these are chosen">
        <ul className="space-y-1 text-sm text-slate-400">
          <li>• Universe: the S&P 500 plus your watchlist and holdings.</li>
          <li>• Kept only if liquid (≥ ~$20M/day), moving (ATR 2–8% of price) and structurally clean (near support or the 52-week high).</li>
          <li>• Three setups: pullback in an uptrend, breakout continuation, and oversold bounce.</li>
          <li>• Exact shares, stop and target come from deterministic sizing — never from the AI.</li>
        </ul>
        <p className="mt-3 text-[11px] text-slate-600">{DISCLAIMER}</p>
      </Card>
    </div>
  );
}
