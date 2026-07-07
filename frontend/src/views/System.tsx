// System view (spec §7.8): provider health, market/scheduler status, cost
// meter (LLM tokens + API calls), event log.

import { useEffect, useState } from "react";
import { api, ProviderCheck } from "../lib/api";
import { PROVIDERS } from "../lib/providers";
import { Button, Card, ErrorNote, Pill, Spinner, fmtWhen } from "../components/ui";

interface Status {
  market_open: boolean;
  todays_session_utc: string[] | null;
  bars_stored: number;
  newest_daily_bar: string | null;
}

interface CostRow {
  day: string;
  provider: string;
  calls: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
}

interface Event {
  ts: string;
  level: string;
  kind: string;
  message: string;
}

export default function System() {
  const [status, setStatus] = useState<Status | null>(null);
  const [costs, setCosts] = useState<CostRow[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [checks, setChecks] = useState<Record<string, ProviderCheck | "testing">>({});
  const [error, setError] = useState("");

  useEffect(() => {
    api.get<Status>("/api/system/status").then(setStatus).catch((e) => setError(String(e.message ?? e)));
    api.get<CostRow[]>("/api/system/costs?days=7").then(setCosts).catch(() => setCosts([]));
    api.get<Event[]>("/api/system/events?limit=50").then(setEvents).catch(() => setEvents([]));
  }, []);

  const testProvider = async (id: string) => {
    setChecks((c) => ({ ...c, [id]: "testing" }));
    try {
      const result = await api.post<ProviderCheck>(`/api/providers/${id}/test`);
      setChecks((c) => ({ ...c, [id]: result }));
    } catch (e) {
      setChecks((c) => ({ ...c, [id]: { provider: id, ok: false, detail: e instanceof Error ? e.message : "failed" } }));
    }
  };

  const totalCost = costs.reduce((sum, r) => sum + r.cost_usd, 0);
  const totalTokens = costs.reduce((sum, r) => sum + r.tokens_in + r.tokens_out, 0);

  if (error) return <ErrorNote message={error} />;

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">System</h1>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <span className="text-xs uppercase tracking-wider text-slate-500">Market</span>
          <p className="mt-1 text-xl font-semibold">{status ? (status.market_open ? "🟢 Open" : "⚪ Closed") : "…"}</p>
        </Card>
        <Card>
          <span className="text-xs uppercase tracking-wider text-slate-500">Bars stored</span>
          <p className="mt-1 text-xl font-semibold">{status?.bars_stored?.toLocaleString() ?? "…"}</p>
        </Card>
        <Card>
          <span className="text-xs uppercase tracking-wider text-slate-500">LLM+API cost (7d)</span>
          <p className="mt-1 text-xl font-semibold">${totalCost.toFixed(2)}</p>
        </Card>
        <Card>
          <span className="text-xs uppercase tracking-wider text-slate-500">Tokens (7d)</span>
          <p className="mt-1 text-xl font-semibold">{totalTokens.toLocaleString()}</p>
        </Card>
      </div>

      <Card title="Provider health">
        <ul className="divide-y divide-slate-800">
          {PROVIDERS.map((p) => {
            const check = checks[p.id];
            return (
              <li key={p.id} className="flex items-center gap-3 py-2 text-sm">
                <span className="w-44 font-medium">{p.name}</span>
                {check === "testing" ? (
                  <Spinner label="testing…" />
                ) : check ? (
                  <Pill ok={check.ok}>{check.ok ? "healthy" : check.detail || "failed"}</Pill>
                ) : (
                  <span className="text-xs text-slate-500">not tested</span>
                )}
                <span className="ml-auto">
                  <Button variant="ghost" onClick={() => testProvider(p.id)} disabled={check === "testing"}>
                    Test
                  </Button>
                </span>
              </li>
            );
          })}
        </ul>
      </Card>

      <Card title="Event log">
        {events.length === 0 ? (
          <p className="text-sm text-slate-500">No events recorded yet.</p>
        ) : (
          <ul className="max-h-96 space-y-1 overflow-y-auto font-mono text-xs">
            {events.map((e, i) => (
              <li key={i} className="flex gap-2">
                <span className="shrink-0 text-slate-600">{fmtWhen(e.ts)}</span>
                <span className={`shrink-0 ${e.level === "ERROR" ? "text-rose-400" : e.level === "WARNING" ? "text-amber-400" : "text-slate-500"}`}>
                  {e.level}
                </span>
                <span className="shrink-0 text-sky-400">{e.kind}</span>
                <span className="text-slate-300">{e.message}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
