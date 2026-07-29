// Thin fetch wrapper for the B-Quant API. Session cookie rides along
// automatically (same origin / vite proxy). 401 responses surface as
// ApiError(401) so the app can bounce to the login screen.

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(resp.status, detail);
  }
  return resp.json() as Promise<T>;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body) }),
};

// ---- shared shapes (mirror the FastAPI routers) ----

export interface SignalSummary {
  id: string;
  created_at: string;
  ticker: string;
  action: "BUY" | "SELL" | "HOLD" | "NO_TRADE";
  shares: number | null;
  max_entry_price: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  confidence: number;
  expected_return_pct: number | null;
  risk_score: number;
  time_horizon: string;
  strategy: string;
  regime: string;
  book?: string; // "core" (long-term) or "swing"
  explanation: string;
  deterministic_only: boolean;
  alert_sent: boolean;
  user_decision: string | null;
}

export interface SwingFeed {
  regime: string | null;
  generated_at: string | null;
  universe_size: number | null;
  scanned: number | null;
  screened_count: number | null;
  alerts_sent: number | null;
  signals: SignalSummary[];
  disclaimer: string;
}

export interface RiskRule {
  rule: string;
  passed: boolean;
  value: number | null;
  limit: number | null;
  detail: string;
}

export interface SignalDetail extends SignalSummary {
  evidence: { source: string; datapoint: string; timestamp: string | null }[];
  risk_check: {
    approved: boolean;
    profile_version: number;
    checked_at: string;
    rules: RiskRule[];
  } | null;
}

export interface PortfolioValuation {
  equity: number;
  cash: number;
  high_water_mark: number;
  day_pnl: number;
  gross_exposure_pct: number;
  positions: {
    symbol: string;
    shares: number;
    cost_basis: number;
    mark: number;
    market_value: number;
    unrealized_pnl: number;
    sector: string;
    weight_pct: number;
  }[];
  sector_weights: Record<string, number>;
}

export interface AppSettings {
  watchlist: string[];
  universe_size: number;
  universe_files: string[];
  starting_equity: number;
  alert_quiet_hours: { start: string; end: string } | null;
  onboarding_complete: boolean;
  operating_mode: OperatingMode;
  operating_mode_label: string;
  full_universe_deep_ingest: boolean;
  focus_set_size: number;
}

export type OperatingMode = "free" | "smart" | "research";

export interface ModeOption {
  mode: OperatingMode;
  label: string;
  description: string;
  scan_depth: "none" | "review" | "full";
  on_demand_depth: "none" | "review" | "full";
  max_llm_candidates_per_scan: number;
}

// Deterministic position management — produced with zero AI cost, in every mode.
export type PositionAction =
  | "EXIT"
  | "REDUCE"
  | "TAKE_PARTIAL_PROFITS"
  | "TIGHTEN_STOP"
  | "INCREASE"
  | "HOLD"
  | "NO_ACTION";

export interface PositionReview {
  symbol: string;
  action: PositionAction;
  shares: number;
  shares_delta: number;
  mark: number;
  cost_basis: number;
  market_value: number;
  weight_pct: number;
  unrealized_pct: number;
  r_multiple: number | null;
  suggested_stop: number | null;
  sector: string;
  reasons: string[];
  urgency: number;
}

export interface BudgetStatus {
  operating_mode: OperatingMode;
  operating_mode_label: string;
  scan_depth: string;
  max_llm_candidates_per_scan: number;
  today: {
    cost_usd: number;
    cost_budget_usd: number;
    calls: number;
    call_budget: number;
    tokens: number;
    token_budget: number;
    degraded: boolean;
  };
  cache: { entries: number; live: number; expired: number; by_kind: Record<string, number> };
}

export interface ProviderOverview {
  fields: Record<string, string[]>;
  configured: Record<string, Record<string, string | null>>;
}

export interface ProviderCheck {
  provider: string;
  ok: boolean;
  detail: string;
}

export const DISCLAIMER =
  "Informational only — not financial advice. Past performance does not " +
  "guarantee future results. You are solely responsible for all trades.";
