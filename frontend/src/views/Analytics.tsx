// Analytics (spec §7.6). Signal-mix stats are live now; outcome stats
// (hit rate, expectancy, Sharpe/Sortino, calibration) unlock once the
// Phase 6 nightly evaluation loop has resolved signals.

import { useEffect, useState } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../lib/api";
import { Card, ErrorNote, Spinner } from "../components/ui";

interface Summary {
  signals_total: number;
  by_action: Record<string, number>;
  by_strategy: Record<string, number>;
  by_regime: Record<string, number>;
  by_decision: Record<string, number>;
  resolved: {
    count: number;
    hit_rate: number | null;
    expectancy_r: number | null;
    sharpe: number | null;
    sortino: number | null;
    brier_score: number | null;
    note: string;
  };
}

function DistChart({ data, color }: { data: Record<string, number>; color: string }) {
  const rows = Object.entries(data).map(([name, count]) => ({ name, count }));
  if (rows.length === 0) return <p className="text-sm text-slate-500">No data yet.</p>;
  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={rows}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
        <XAxis dataKey="name" stroke="#64748b" fontSize={11} />
        <YAxis allowDecimals={false} stroke="#64748b" fontSize={11} />
        <Tooltip
          contentStyle={{ backgroundColor: "#0f172a", border: "1px solid #334155", fontSize: 12 }}
          labelStyle={{ color: "#e2e8f0" }}
        />
        <Bar dataKey="count" fill={color} radius={[4, 4, 0, 0]} />
      </BarChart>
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

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        {[
          ["Signals total", String(summary.signals_total)],
          ["Resolved", String(outcome.count)],
          ["Hit rate", outcome.hit_rate === null ? "—" : `${(outcome.hit_rate * 100).toFixed(1)}%`],
          ["Expectancy (R)", outcome.expectancy_r === null ? "—" : outcome.expectancy_r.toFixed(2)],
          ["Brier score", outcome.brier_score === null ? "—" : outcome.brier_score.toFixed(3)],
        ].map(([label, value]) => (
          <Card key={label}>
            <span className="text-xs uppercase tracking-wider text-slate-500">{label}</span>
            <p className="mt-1 text-xl font-semibold">{value}</p>
          </Card>
        ))}
      </div>

      {outcome.count === 0 && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 text-sm text-amber-300">
          {outcome.note}
        </div>
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
