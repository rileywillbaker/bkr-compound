// Portfolio (spec §7.3): manual trade entry, live valuation, sector exposure,
// recent fills. All fills are user-entered — B-Quant never executes trades.

import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, PortfolioValuation } from "../lib/api";
import { Button, Card, ErrorNote, Field, Spinner, fmtMoney, fmtWhen, inputClass } from "../components/ui";

interface Trade {
  id: number;
  ts: string;
  symbol: string;
  side: string;
  shares: number;
  price: number;
  note: string;
}

export default function Portfolio() {
  const [valuation, setValuation] = useState<PortfolioValuation | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [error, setError] = useState("");
  const [form, setForm] = useState({ symbol: "", side: "BUY", shares: "", price: "", note: "" });
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState("");

  const refresh = useCallback(() => {
    Promise.all([api.get<PortfolioValuation>("/api/portfolio"), api.get<Trade[]>("/api/portfolio/trades?limit=25")])
      .then(([v, t]) => {
        setValuation(v);
        setTrades(t);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "load failed"));
  }, []);

  useEffect(refresh, [refresh]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setFormError("");
    try {
      await api.post("/api/portfolio/trades", {
        symbol: form.symbol.trim().toUpperCase(),
        side: form.side,
        shares: parseInt(form.shares, 10),
        price: parseFloat(form.price),
        note: form.note,
      });
      setForm({ symbol: "", side: "BUY", shares: "", price: "", note: "" });
      refresh();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "save failed");
    } finally {
      setBusy(false);
    }
  };

  if (error) return <ErrorNote message={error} />;
  if (!valuation) return <Spinner />;

  const drawdownPct =
    valuation.high_water_mark > 0
      ? ((valuation.high_water_mark - valuation.equity) / valuation.high_water_mark) * 100
      : 0;

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">Portfolio</h1>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {[
          ["Equity", fmtMoney(valuation.equity), ""],
          ["Cash", fmtMoney(valuation.cash), ""],
          ["Drawdown from HWM", `${drawdownPct.toFixed(2)}%`, drawdownPct > 5 ? "text-rose-400" : ""],
          ["Gross exposure", `${valuation.gross_exposure_pct.toFixed(1)}%`, ""],
        ].map(([label, value, cls]) => (
          <Card key={label}>
            <span className="text-xs uppercase tracking-wider text-slate-500">{label}</span>
            <p className={`mt-1 text-xl font-semibold ${cls}`}>{value}</p>
          </Card>
        ))}
      </div>

      <Card title="Open positions">
        {valuation.positions.length === 0 ? (
          <p className="text-sm text-slate-500">No positions. Record your fills below and valuation goes live.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-slate-500">
                  <th className="py-1 pr-3">Symbol</th>
                  <th className="py-1 pr-3">Shares</th>
                  <th className="py-1 pr-3">Cost basis</th>
                  <th className="py-1 pr-3">Mark</th>
                  <th className="py-1 pr-3">Value</th>
                  <th className="py-1 pr-3">Unrealized</th>
                  <th className="py-1 pr-3">Weight</th>
                  <th className="py-1">Sector</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {valuation.positions.map((p) => (
                  <tr key={p.symbol}>
                    <td className="py-1.5 pr-3 font-mono font-semibold">{p.symbol}</td>
                    <td className="py-1.5 pr-3">{p.shares}</td>
                    <td className="py-1.5 pr-3">{fmtMoney(p.cost_basis)}</td>
                    <td className="py-1.5 pr-3">{fmtMoney(p.mark)}</td>
                    <td className="py-1.5 pr-3">{fmtMoney(p.market_value)}</td>
                    <td className={`py-1.5 pr-3 ${p.unrealized_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                      {fmtMoney(p.unrealized_pnl)}
                    </td>
                    <td className="py-1.5 pr-3">{p.weight_pct.toFixed(1)}%</td>
                    <td className="py-1.5 text-slate-400">{p.sector || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {Object.keys(valuation.sector_weights).length > 0 && (
          <div className="mt-3 flex flex-wrap gap-2 border-t border-slate-800 pt-3">
            {Object.entries(valuation.sector_weights).map(([sector, weight]) => (
              <span key={sector} className="rounded-full bg-slate-800 px-3 py-1 text-xs text-slate-300">
                {sector}: {weight.toFixed(1)}%
              </span>
            ))}
          </div>
        )}
      </Card>

      <Card title="Record a fill (manual entry)">
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-5">
          <Field label="Ticker">
            <input className={inputClass} value={form.symbol} onChange={(e) => setForm({ ...form, symbol: e.target.value })} placeholder="NVDA" />
          </Field>
          <Field label="Side">
            <select className={inputClass} value={form.side} onChange={(e) => setForm({ ...form, side: e.target.value })}>
              <option>BUY</option>
              <option>SELL</option>
            </select>
          </Field>
          <Field label="Shares">
            <input className={inputClass} value={form.shares} onChange={(e) => setForm({ ...form, shares: e.target.value })} inputMode="numeric" />
          </Field>
          <Field label="Fill price">
            <input className={inputClass} value={form.price} onChange={(e) => setForm({ ...form, price: e.target.value })} inputMode="decimal" />
          </Field>
          <div className="flex items-end">
            <Button type="submit" disabled={busy || !form.symbol || !form.shares || !form.price}>
              {busy ? "Saving…" : "Record"}
            </Button>
          </div>
        </form>
        {formError && <p className="mt-2 text-sm text-rose-400">{formError}</p>}
      </Card>

      <Card title="Recent fills">
        {trades.length === 0 ? (
          <p className="text-sm text-slate-500">No fills recorded yet.</p>
        ) : (
          <ul className="divide-y divide-slate-800 text-sm">
            {trades.map((t) => (
              <li key={t.id} className="flex items-center gap-3 py-2">
                <span className={`font-semibold ${t.side === "BUY" ? "text-emerald-400" : "text-rose-400"}`}>{t.side}</span>
                <span className="font-mono">{t.symbol}</span>
                <span className="text-slate-400">{t.shares} @ {fmtMoney(t.price)}</span>
                {t.note && <span className="text-xs text-slate-500">“{t.note}”</span>}
                <span className="ml-auto text-xs text-slate-500">{fmtWhen(t.ts)}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
