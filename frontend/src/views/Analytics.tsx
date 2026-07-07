// Analytics (spec §7.6): signal mix, outcome stats from the nightly
// evaluation loop, confidence-calibration plot (predicted vs realized),
// per-strategy/per-regime performance, missed-opportunity log.

import { useEffect, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../lib/api";
import { Card, ErrorNote, Spinner, fmtPct } from "../components/ui";

interface CalibrationPoint {
  predicted: number;
  realized: number;
  count: number;
}

interface StrategyStat {
  strategy: string;
  regime: string;
  resolved_count: number;
  hit_rate: number;
  expectancy_r: number;
}

interface Summary {
  signals_total: number;
  by_action: Record<string, number>;
  by_strategy: Record<string, number>;
  by_regime: Record<string, number>;
  by_decision: Record<string, number>;
  strategy_stats: StrategyStat[];
  resolved: {
    count: number;
    hit_rate: number | null;
    expectancy_r: number | null;
    sharpe: number | null;
    sortino: number | null;
    brier_score: number | null;
    calibration: CalibrationPoint[];
    missed_wins: { signal_id: string; ticker: string; return_pct: number; r_multiple: number }[];
    note: string;
  };
}

const tooltipStyle = {
  contentStyle: { backgroundColor: "#0f172a", border: "1px solid #334155", fontSize: 12 },
  labelStyle: { color: "#e2e8f0" },
};

function DistChart({ data, color }: { data: Record<string, number>; color: string }) {
  const rows = Object.entries(data).map(([name, count]) => ({ name, count }));
  if (rows.length === 0) return <p className="text-sm text-slate-500">No data yet.</p>;
  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={rows}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
        <XAxis dataKey="name" stroke="#64748b" fontSize={11} />
        <YAxis allowDecimals={false} stroke="#64748b" fontSize={11} />
        <Tooltip {...tooltipStyle} />
        <Bar dataKey="count" fill={color} radius={[4, 4, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function CalibrationChart({ points }: { points: CalibrationPoint[] }) {
  const data = points.map((p) => ({ ...p, ideal: p.predicted }));
  return (
    <ResponsiveContainer width="100%" height={240}>
      <LineChart data={data}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
        <XAxis dataKey="predicted" stroke="#64748b" fontSize={11} domain={[0, 1]} />
        <YAxis stroke="#64748b" fontSize={11} domain={[0, 1]} />
        <Tooltip {...tooltipStyle} />
        <Line type="monotone" dataKey="ideal" stroke="#475569" strokeDasharray="4 4" dot={false} name="perfect" />
        <Line type="monotone" dataKey="realized" stroke="#38bdf8" strokeWidth={2} name="realized" />
      </LineChart>
    </ResponsiveContainer>
  );
}

export default function Analytics() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .get<Summary>("/api/analytics/summary")
      .then(setSummary)
      .catch((e) => setError(e instanceof Error ? e.message : "load failed"));
  }, []);

  if (error) return <ErrorNote message={error} />;
  if (!summary) return <Spinner />;

  const outcome = summary.resolved;

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">Analytics</h1>

      <div className="grid gap-4 sm:grid-cols-3 lg:grid-cols-6">
        {[
          ["Signals", String(summary.signals_total)],
          ["Resolved", String(outcome.count)],
          ["Hit rate", outcome.hit_rate === null ? "—" : `${(outcome.hit_rate * 100).toFixed(1)}%`],
          ["Expectancy (R)", outcome.expectancy_r === null ? "—" : outcome.expectancy_r.toFixed(2)],
          ["Sharpe / Sortino", `${outcome.sharpe ?? "—"} / ${outcome.sortino ?? "—"}`],
          ["Brier score", outcome.brier_score === null ? "—" : outcome.brier_score.toFixed(3)],
        ].map(([label, value]) => (
          <Card key={label}>
            <span className="text-xs uppercase tracking-wider text-slate-500">{label}</span>
            <p className="mt-1 text-lg font-semibold">{value}</p>
          </Card>
        ))}
      </div>

      {outcome.count === 0 && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 text-sm text-amber-300">
          No resolved signals yet — the nightly job resolves BUY signals when their stop, target, or
          horizon expiry is hit. {outcome.note}
        </div>
      )}

      {outcome.calibration.length > 0 && (
        <Card title="Confidence calibration (predicted vs realized win rate)">
          <CalibrationChart points={outcome.calibration} />
        </Card>
      )}

      {summary.strategy_stats.length > 0 && (
        <Card title="Performance by strategy and regime">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-slate-500">
                  <th className="py-1 pr-3">Strategy</th>
                  <th className="py-1 pr-3">Regime</th>
                  <th className="py-1 pr-3">Resolved</th>
                  <th className="py-1 pr-3">Hit rate</th>
                  <th className="py-1">Expectancy (R)</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {summary.strategy_stats.map((s) => (
                  <tr key={`${s.strategy}-${s.regime}`}>
                    <td className="py-1.5 pr-3 font-mono">{s.strategy}</td>
                    <td className="py-1.5 pr-3">{s.regime === "*" ? "all" : s.regime}</td>
                    <td className="py-1.5 pr-3">{s.resolved_count}</td>
                    <td className="py-1.5 pr-3">{fmtPct(s.hit_rate * 100)}</td>
                    <td className={`py-1.5 ${s.expectancy_r >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                      {s.expectancy_r.toFixed(2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {outcome.missed_wins.length > 0 && (
        <Card title="Missed opportunities (skipped signals that won)">
          <ul className="divide-y divide-slate-800 text-sm">
            {outcome.missed_wins.map((m) => (
              <li key={m.signal_id} className="flex items-center gap-3 py-2">
                <span className="font-mono font-semibold">{m.ticker}</span>
                <span className="text-emerald-400">+{m.return_pct.toFixed(1)}%</span>
                <span className="text-slate-400">{m.r_multiple.toFixed(1)}R</span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Signals by action"><DistChart data={summary.by_action} color="#38bdf8" /></Card>
        <Card title="Signals by strategy"><DistChart data={summary.by_strategy} color="#a78bfa" /></Card>
        <Card title="Signals by regime"><DistChart data={summary.by_regime} color="#34d399" /></Card>
        <Card title="Your decisions"><DistChart data={summary.by_decision} color="#fbbf24" /></Card>
      </div>
    </div>
  );
}
