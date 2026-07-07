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
  explanation: string;
  deterministic_only: boolean;
  alert_sent: boolean;
  user_decision: string | null;
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
  starting_equity: number;
  alert_quiet_hours: { start: string; end: string } | null;
  onboarding_complete: boolean;
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
