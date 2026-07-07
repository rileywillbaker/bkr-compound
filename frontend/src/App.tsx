// Phase 0 placeholder shell. Phase 5 replaces this with the full app
// (Chat, Dashboard, Portfolio, Signals, Journal, Analytics, Settings, System).
export const DISCLAIMER =
  "Informational only — not financial advice. Past performance does not " +
  "guarantee future results. You are solely responsible for all trades.";

export default function App() {
  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col items-center justify-center gap-4 p-8">
      <h1 className="text-3xl font-bold tracking-tight">B-Quant</h1>
      <p className="text-slate-400">Stack is up. Application UI arrives in Phase 5.</p>
      <p className="max-w-md text-center text-xs text-slate-500">{DISCLAIMER}</p>
    </div>
  );
}
