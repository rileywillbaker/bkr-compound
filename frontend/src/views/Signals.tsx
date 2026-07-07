// Signals feed (spec §7.4): live via WebSocket, filterable history; each
// signal expands to evidence + the complete risk-check table; the user marks
// taken / skipped / modified (that's the only "action" — nothing executes).

import { useCallback, useEffect, useState } from "react";
import { api, SignalDetail, SignalSummary } from "../lib/api";
import { useFeed } from "../lib/ws";
import { ActionBadge, Button, Card, ErrorNote, Pill, Spinner, fmtMoney, fmtWhen } from "../components/ui";

const inputSm =
  "rounded-lg border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-100 outline-none focus:border-sky-500";

function RiskTable({ detail }: { detail: SignalDetail }) {
  if (!detail.risk_check) return <p className="text-xs text-slate-500">No risk check (informational signal).</p>;
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

function SignalRowView({ signal, onDecided }: { signal: SignalSummary; onDecided: () => void }) {
  const [detail, setDetail] = useState<SignalDetail | null>(null);
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const toggle = async () => {
    if (!open && !detail) setDetail(await api.get<SignalDetail>(`/api/signals/${signal.id}`));
    setOpen(!open);
  };

  const decide = async (decision: "taken" | "skipped" | "modified") => {
    setBusy(true);
    try {
      await api.post(`/api/signals/${signal.id}/decision`, { decision, note });
      onDecided();
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="py-2">
      <button onClick={toggle} className="flex w-full items-center gap-3 text-left text-sm">
        <ActionBadge action={signal.action} />
        <span className="font-mono font-semibold">{signal.ticker}</span>
        <span className="text-slate-400">
          {(signal.confidence * 100).toFixed(0)}% · risk {signal.risk_score}/10 · {signal.strategy}
        </span>
        {signal.deterministic_only && <Pill ok={false}>deterministic-only</Pill>}
        {signal.user_decision && <Pill ok>{signal.user_decision}</Pill>}
        <span className="ml-auto text-xs text-slate-500">{fmtWhen(signal.created_at)}</span>
        <span className="text-slate-600">{open ? "▾" : "▸"}</span>
      </button>

      {open && detail && (
        <div className="mt-3 space-y-4 rounded-lg border border-slate-800 bg-slate-950 p-4">
          {(signal.action === "BUY" || signal.action === "SELL") && (
            <div className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
              <div><span className="text-xs text-slate-500">Shares</span><p className="font-semibold">{detail.shares ?? "—"}</p></div>
              <div><span className="text-xs text-slate-500">Max entry</span><p className="font-semibold">{fmtMoney(detail.max_entry_price)}</p></div>
              <div><span className="text-xs text-slate-500">Stop loss</span><p className="font-semibold">{fmtMoney(detail.stop_loss)}</p></div>
              <div><span className="text-xs text-slate-500">Target</span><p className="font-semibold">{fmtMoney(detail.take_profit)}</p></div>
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

          {!signal.user_decision && (
            <div className="flex flex-wrap items-center gap-2 border-t border-slate-800 pt-3">
              <span className="text-xs text-slate-500">Your decision:</span>
              <input
                className={`${inputSm} w-64`}
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

export default function Signals() {
  const [signals, setSignals] = useState<SignalSummary[] | null>(null);
  const [error, setError] = useState("");
  const [ticker, setTicker] = useState("");
  const [action, setAction] = useState("");
  const [live, setLive] = useState(0);

  const load = useCallback(() => {
    const params = new URLSearchParams();
    if (ticker.trim()) params.set("ticker", ticker.trim().toUpperCase());
    if (action) params.set("action", action);
    params.set("limit", "100");
    api
      .get<{ signals: SignalSummary[] }>(`/api/signals?${params}`)
      .then((b) => setSignals(b.signals))
      .catch((e) => setError(e instanceof Error ? e.message : "load failed"));
  }, [ticker, action]);

  useEffect(load, [load]);
  useFeed((event) => {
    if (event.kind === "signal") {
      setLive((n) => n + 1);
      load();
    }
  });

  if (error) return <ErrorNote message={error} />;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold">Signals</h1>
        <span className="flex items-center gap-1 text-xs text-emerald-400">
          <span className="h-2 w-2 animate-pulse rounded-full bg-emerald-400" /> live{live > 0 ? ` · ${live} new` : ""}
        </span>
        <div className="ml-auto flex gap-2">
          <input className={inputSm} placeholder="ticker" value={ticker} onChange={(e) => setTicker(e.target.value)} />
          <select className={inputSm} value={action} onChange={(e) => setAction(e.target.value)}>
            <option value="">all actions</option>
            {["BUY", "SELL", "HOLD", "NO_TRADE"].map((a) => (
              <option key={a} value={a}>{a}</option>
            ))}
          </select>
        </div>
      </div>

      <Card>
        {!signals ? (
          <Spinner />
        ) : signals.length === 0 ? (
          <p className="text-sm text-slate-500">No signals match. NO TRADE days are normal and expected.</p>
        ) : (
          <ul className="divide-y divide-slate-800">
            {signals.map((s) => (
              <SignalRowView key={s.id} signal={s} onDecided={load} />
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
