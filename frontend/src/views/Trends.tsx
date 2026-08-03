// Trend Discovery: emerging themes, the evidence behind each score, and the
// risk-approved dollar recommendations that came out of them.
//
// The view deliberately shows what was REJECTED alongside what was picked. A
// screen that only ever displays winners teaches the user nothing about the
// system's judgement, and this one refuses far more often than it recommends.

import { useCallback, useEffect, useState } from "react";
import { Button, Card, ErrorNote, Spinner, fmtMoney, fmtPct, fmtWhen } from "../components/ui";
import { api } from "../lib/api";

type Component = { score: number; weight: number; detail: string; covered: boolean };

type Trend = {
  theme: string;
  name: string;
  score: number;
  legitimacy: string;
  explanation: string;
  hype_flags: string[];
  components: Record<string, Component>;
  symbols: string[];
  coverage_gaps: string[];
  computed_at: string | null;
};

type Allocation = {
  approved: boolean;
  dollars: number;
  shares: number;
  fractional_required: boolean;
  stop_loss: number | null;
  take_profit: number | null;
  reasons: string[];
  failed_rules: string[];
  cash_available: number;
};

type Opportunity = {
  symbol: string;
  company: string;
  sector: string;
  price: number | null;
  theme_name: string;
  theme_score: number;
  why_selected: string;
  trend_connection: string;
  bullish: string[];
  bearish: string[];
  risk_level: string;
  confidence: number;
  factors: Record<string, number>;
  allocation: Allocation | null;
};

type ExcludedStock = { symbol: string; exclusion_reason: string };

type Report = {
  day: string;
  market_environment: string;
  market_environment_detail: string;
  opportunities: Opportunity[];
  rejected: Opportunity[];
  excluded_stocks: ExcludedStock[];
  portfolio: Record<string, unknown>;
  llm_used: boolean;
  llm_calls: number;
  mode: string;
  notes: string[];
};

const LEGITIMACY_STYLE: Record<string, string> = {
  legitimate: "bg-emerald-500/15 text-emerald-400 border-emerald-500/40",
  emerging: "bg-sky-500/15 text-sky-300 border-sky-500/40",
  mixed: "bg-amber-500/15 text-amber-400 border-amber-500/40",
  hype: "bg-rose-500/15 text-rose-400 border-rose-500/40",
  unproven: "bg-slate-500/15 text-slate-400 border-slate-500/40",
};

const RISK_STYLE: Record<string, string> = {
  Low: "text-emerald-400",
  Medium: "text-amber-400",
  High: "text-rose-400",
};

const ENVIRONMENT_STYLE: Record<string, string> = {
  Bullish: "text-emerald-400",
  Neutral: "text-amber-400",
  Bearish: "text-rose-400",
};

const COMPONENT_LABELS: Record<string, string> = {
  news_momentum: "News",
  policy_support: "Policy",
  market_confirmation: "Market",
  etf_activity: "ETF flows",
  social_attention: "Social",
  breadth: "Breadth",
};

function ScoreBar({ score, legitimacy }: { score: number; legitimacy: string }) {
  const tone =
    legitimacy === "hype" ? "bg-rose-500" : legitimacy === "legitimate" ? "bg-emerald-500" : "bg-sky-500";
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-slate-800">
      <div className={`h-full ${tone}`} style={{ width: `${Math.max(0, Math.min(100, score))}%` }} />
    </div>
  );
}

function TrendCard({ trend }: { trend: Trend }) {
  const [open, setOpen] = useState(false);
  const components = Object.entries(trend.components);

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="font-semibold text-slate-100">{trend.name}</h3>
          <span
            className={`mt-1 inline-block rounded border px-2 py-0.5 text-[11px] font-medium ${
              LEGITIMACY_STYLE[trend.legitimacy] ?? LEGITIMACY_STYLE.unproven
            }`}
          >
            {trend.legitimacy}
          </span>
        </div>
        <div className="shrink-0 text-right">
          <div className="text-2xl font-bold tabular-nums text-slate-100">{trend.score.toFixed(0)}</div>
          <div className="text-[10px] uppercase tracking-wider text-slate-500">/ 100</div>
        </div>
      </div>

      <div className="mt-3">
        <ScoreBar score={trend.score} legitimacy={trend.legitimacy} />
      </div>

      <p className="mt-3 text-sm leading-relaxed text-slate-300">{trend.explanation}</p>

      {trend.hype_flags.length > 0 && (
        <ul className="mt-3 space-y-1 rounded-lg border border-amber-500/30 bg-amber-500/5 p-2">
          {trend.hype_flags.map((flag) => (
            <li key={flag} className="text-xs text-amber-300">
              ⚠ {flag}
            </li>
          ))}
        </ul>
      )}

      <button
        onClick={() => setOpen(!open)}
        className="mt-3 text-xs text-sky-400 hover:text-sky-300"
      >
        {open ? "Hide" : "Show"} score breakdown
      </button>

      {open && (
        <div className="mt-3 space-y-2 border-t border-slate-800 pt-3">
          {components.map(([name, component]) => (
            <div key={name}>
              <div className="flex items-baseline justify-between text-xs">
                <span className="font-medium text-slate-300">{COMPONENT_LABELS[name] ?? name}</span>
                <span className="tabular-nums text-slate-400">
                  {component.covered ? `${component.score.toFixed(0)} × ${component.weight.toFixed(0)}%` : "not measured"}
                </span>
              </div>
              <p className="text-[11px] leading-snug text-slate-500">{component.detail}</p>
            </div>
          ))}
          {trend.symbols.length > 0 && (
            <p className="pt-1 text-[11px] text-slate-500">
              <span className="text-slate-400">Constituents considered:</span> {trend.symbols.slice(0, 18).join(", ")}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function OpportunityCard({ opportunity }: { opportunity: Opportunity }) {
  const allocation = opportunity.allocation;
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <span className="text-lg font-bold text-slate-100">{opportunity.symbol}</span>
          <span className="ml-2 text-sm text-slate-400">{opportunity.company}</span>
        </div>
        {allocation?.approved && (
          <div className="text-right">
            <div className="text-xl font-bold tabular-nums text-emerald-400">{fmtMoney(allocation.dollars)}</div>
            <div className="text-[10px] text-slate-500">
              {allocation.fractional_required
                ? "fractional shares required"
                : `${allocation.shares} share${allocation.shares === 1 ? "" : "s"}`}
            </div>
          </div>
        )}
      </div>

      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400">
        <span>Price: {fmtMoney(opportunity.price)}</span>
        <span>
          Risk: <span className={RISK_STYLE[opportunity.risk_level] ?? ""}>{opportunity.risk_level}</span>
        </span>
        <span>Confidence: {(opportunity.confidence * 100).toFixed(0)}%</span>
        <span>
          Theme: {opportunity.theme_name} ({opportunity.theme_score.toFixed(0)})
        </span>
      </div>

      <p className="mt-3 text-sm text-slate-300">{opportunity.why_selected}</p>

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <div>
          <h4 className="text-[10px] font-semibold uppercase tracking-wider text-emerald-500">Bullish</h4>
          <ul className="mt-1 space-y-0.5">
            {opportunity.bullish.length === 0 && <li className="text-xs text-slate-600">—</li>}
            {opportunity.bullish.map((point) => (
              <li key={point} className="text-xs text-slate-400">
                + {point}
              </li>
            ))}
          </ul>
        </div>
        <div>
          <h4 className="text-[10px] font-semibold uppercase tracking-wider text-rose-500">Bearish</h4>
          <ul className="mt-1 space-y-0.5">
            {opportunity.bearish.length === 0 && <li className="text-xs text-slate-600">—</li>}
            {opportunity.bearish.map((point) => (
              <li key={point} className="text-xs text-slate-400">
                − {point}
              </li>
            ))}
          </ul>
        </div>
      </div>

      {allocation?.approved && (
        <div className="mt-3 flex flex-wrap gap-x-4 border-t border-slate-800 pt-2 text-xs text-slate-500">
          <span>Stop loss: {fmtMoney(allocation.stop_loss)}</span>
          <span>Target: {fmtMoney(allocation.take_profit)}</span>
          <span>Cash available: {fmtMoney(allocation.cash_available)}</span>
        </div>
      )}
    </div>
  );
}

export default function Trends() {
  const [trends, setTrends] = useState<Trend[]>([]);
  const [report, setReport] = useState<Report | null>(null);
  const [text, setText] = useState("");
  const [asOf, setAsOf] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<"" | "collect" | "report">("");
  const [error, setError] = useState("");
  const [showMessage, setShowMessage] = useState(false);

  const load = useCallback(async () => {
    setError("");
    try {
      const [trendBody, reportBody] = await Promise.all([
        api.get<{ trends: Trend[]; as_of: string | null }>("/api/trends"),
        api.get<{ report: Report | null; text: string; created_at: string | null }>("/api/trends/report"),
      ]);
      setTrends(trendBody.trends);
      setAsOf(trendBody.as_of);
      setReport(reportBody.report);
      setText(reportBody.text);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load trends");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const run = async (kind: "collect" | "report") => {
    setBusy(kind);
    setError("");
    try {
      await api.post(kind === "collect" ? "/api/trends/collect" : "/api/trends/report", {});
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : `failed to run ${kind}`);
    } finally {
      setBusy("");
    }
  };

  if (loading) return <Spinner label="Loading trends…" />;

  const environment = report?.market_environment ?? "Neutral";

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Trend Discovery</h1>
          <p className="text-sm text-slate-400">
            Emerging themes from free public sources — news, government filings, market data, ETF activity and
            social discussion. Every recommendation is sized by the risk engine.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="ghost" onClick={() => run("collect")} disabled={busy !== ""}>
            {busy === "collect" ? "Collecting…" : "Collect sources"}
          </Button>
          <Button onClick={() => run("report")} disabled={busy !== ""}>
            {busy === "report" ? "Building…" : "Rebuild report"}
          </Button>
        </div>
      </header>

      {error && <ErrorNote message={error} />}

      {report && (
        <Card title="Market environment">
          <div className="flex flex-wrap items-baseline gap-3">
            <span className={`text-xl font-bold ${ENVIRONMENT_STYLE[environment] ?? ""}`}>{environment}</span>
            <span className="text-sm text-slate-400">{report.market_environment_detail}</span>
          </div>
          <div className="mt-2 flex flex-wrap gap-x-4 text-xs text-slate-500">
            <span>Operating mode: {report.mode}</span>
            <span>AI calls this run: {report.llm_calls}</span>
            <span>Report date: {report.day}</span>
          </div>
          {report.notes.map((note) => (
            <p key={note} className="mt-2 text-xs text-amber-400">
              Note: {note}
            </p>
          ))}
        </Card>
      )}

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">
          Top emerging trends {asOf && <span className="ml-1 normal-case text-slate-600">· {asOf}</span>}
        </h2>
        {trends.length === 0 ? (
          <Card>
            <p className="text-sm text-slate-400">
              No trends scored yet. Run “Collect sources” to gather today’s free data — it takes a few minutes and
              costs nothing.
            </p>
          </Card>
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {trends.slice(0, 8).map((trend) => (
              <TrendCard key={trend.theme} trend={trend} />
            ))}
          </div>
        )}
      </section>

      {report && (
        <section>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-slate-400">Best opportunities</h2>
          {report.opportunities.length === 0 ? (
            <Card>
              <p className="text-sm text-slate-400">
                Nothing recommended today. No name cleared the quality gate, the pump filter and the risk engine at
                an actionable size — which is a normal outcome, not a failure.
              </p>
            </Card>
          ) : (
            <div className="space-y-3">
              {report.opportunities.map((opportunity) => (
                <OpportunityCard key={opportunity.symbol} opportunity={opportunity} />
              ))}
              <Card title="Suggested buys">
                <table className="w-full text-sm">
                  <tbody>
                    {report.opportunities.map((o) => (
                      <tr key={o.symbol} className="border-b border-slate-800 last:border-0">
                        <td className="py-1.5 font-semibold text-emerald-400">BUY {o.symbol}</td>
                        <td className="py-1.5 text-right tabular-nums">{fmtMoney(o.allocation?.dollars)}</td>
                        <td className="py-1.5 pl-4 text-right text-xs text-slate-500">{o.risk_level} risk</td>
                      </tr>
                    ))}
                    <tr>
                      <td className="pt-2 text-xs uppercase tracking-wider text-slate-500">Total proposed</td>
                      <td className="pt-2 text-right font-bold tabular-nums">
                        {fmtMoney(report.opportunities.reduce((sum, o) => sum + (o.allocation?.dollars ?? 0), 0))}
                      </td>
                      <td />
                    </tr>
                  </tbody>
                </table>
              </Card>
            </div>
          )}
        </section>
      )}

      {report && (report.rejected.length > 0 || report.excluded_stocks.length > 0) && (
        <Card title="Considered and declined">
          <p className="mb-2 text-xs text-slate-500">
            What the system looked at and refused, and why. This is as informative as what it picked.
          </p>
          <ul className="space-y-1.5">
            {report.rejected.map((o) => (
              <li key={`r-${o.symbol}`} className="text-xs text-slate-400">
                <span className="font-semibold text-slate-300">{o.symbol}</span>{" "}
                {o.allocation?.failed_rules?.length
                  ? `risk engine declined: ${o.allocation.failed_rules.join(", ")}`
                  : (o.allocation?.reasons ?? []).join("; ") || "no allocation computed"}
              </li>
            ))}
            {report.excluded_stocks.slice(0, 10).map((s) => (
              <li key={`x-${s.symbol}`} className="text-xs text-slate-400">
                <span className="font-semibold text-slate-300">{s.symbol}</span> {s.exclusion_reason}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {text && (
        <Card
          title="Daily message"
          actions={
            <button onClick={() => setShowMessage(!showMessage)} className="text-xs text-sky-400 hover:text-sky-300">
              {showMessage ? "Hide" : "Show"}
            </button>
          }
        >
          {showMessage ? (
            <pre className="overflow-x-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-slate-300">
              {text}
            </pre>
          ) : (
            <p className="text-xs text-slate-500">The exact text sent to Telegram.</p>
          )}
        </Card>
      )}

      {report?.portfolio && Object.keys(report.portfolio).length > 0 && (
        <Card title="Portfolio context">
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm text-slate-300">
            <span>Equity: {fmtMoney(report.portfolio.equity as number)}</span>
            <span>Cash: {fmtMoney(report.portfolio.cash as number)}</span>
            <span>
              Positions: {String(report.portfolio.open_positions)} / {String(report.portfolio.max_open_positions)}
            </span>
            <span>Gross exposure: {fmtPct(report.portfolio.gross_exposure_pct as number)}</span>
          </div>
          <p className="mt-2 text-[11px] text-slate-600">
            Last updated {fmtWhen(asOf ? `${asOf}T00:00:00Z` : null)}
          </p>
        </Card>
      )}
    </div>
  );
}
