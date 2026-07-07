## 1. Mission

Build **B-Quant**, a personal AI-agentic stock analysis and trade-recommendation platform.

Non-negotiable constraints:

1. **The system NEVER executes trades.** No brokerage order endpoints are ever called
   with live orders. Alpaca is used for market data and (optionally) paper-trading
   simulation only. The human user places all real trades manually.
2. **NO TRADE is a first-class output.** The system is optimized for long-term
   risk-adjusted returns and capital preservation, not trade frequency. Most days the
   correct output is nothing.
3. **The Risk Engine is deterministic code with absolute veto authority.** It is NOT an
   LLM. No agent, prompt, or user chat message can override a risk rejection.
4. **Every signal is fully auditable.** All inputs, agent outputs, risk checks, and user
   decisions are logged to the database with timestamps.
5. **Disclaimers everywhere.** The UI and every alert must state that outputs are
   informational, not financial advice, past performance does not guarantee results, and
   the user is solely responsible for all trades.
6. Provide parameter settings via common trading concepts to search for companies based on sector, risk, market cap, etc. the user should be able to set these for the AI models to search

## 2. Locked Technology Stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.12+ | Backend, agents, pipelines |
| API framework | FastAPI + Uvicorn | REST + WebSocket to frontend |
| Agent orchestration | LangGraph | Deterministic graph, typed state, checkpointing |
| LLM abstraction | LiteLLM | Provider-agnostic; models configured in `config/models.yaml`, never hardcoded |
| Default reasoning model | `claude-sonnet-4-6` (Anthropic API) | Synthesis, strategy, explanation |
| Default triage model | `claude-haiku-4-5` | News screening, cheap classification |
| Optional local fallback | Ollama endpoint via LiteLLM | Config-only swap, no code changes |
| Database | PostgreSQL 16 + TimescaleDB extension | Hypertables for bars/quotes |
| Cache/queue | Redis | Rate-limit budgets, pub/sub for live feed |
| Scheduler | APScheduler | Market-hours-aware jobs (America/New_York, holiday calendar) |
| Market data | Alpaca (free, IEX feed) primary; yfinance dev-only fallback | Behind a `MarketDataProvider` interface |
| News/fundamentals/calendars/sentiment | Finnhub free tier | Behind a `ResearchDataProvider` interface |
| Macro data | FRED API (free) | Rates, CPI, unemployment, yield curve |
| Filings | SEC EDGAR (free, respect rate limits + User-Agent rules) | 8-K, 10-Q/K, Form 4 |
| Alerts | Telegram Bot API (primary); Twilio SMS as optional plugin | Same `AlertChannel` interface |
| Frontend | React 18 + Vite + Tailwind, Recharts | Single-page app |
| Deployment | Docker Compose | Runs identically on local mini PC or any VPS |
| Secrets | `.env` + pydantic-settings; never committed | `.env.example` provided |
| Testing | pytest, ≥85% coverage on risk engine (target 100% of its branches) | CI via GitHub Actions |

Provider abstraction rule: every external dependency (LLM, market data, research data,
alert channel) sits behind an interface in `sentinel/providers/`. Swapping providers must
require only config changes.

## 3. System Architecture

```
                        ┌────────────────────────────────────┐
                        │            SCHEDULER               │
                        │  (market-hours aware, ET timezone) │
                        └───────────────┬────────────────────┘
                                        │ triggers scans
┌───────────────┐   ingest   ┌──────────▼──────────┐
│ Data Providers │──────────▶│   DATA LAYER        │  TimescaleDB + Redis
│ Alpaca/Finnhub │           │ bars,news,macro,    │
│ FRED/EDGAR     │           │ fundamentals,events │
└───────────────┘            └──────────┬──────────┘
                                        │ typed MarketContext
                        ┌───────────────▼────────────────────┐
                        │      LANGGRAPH AGENT PIPELINE      │
                        │ Regime → Screener → Analysts (∥) → │
                        │ Strategy Selector → Portfolio/     │
                        │ Sizing → Signal Synthesizer        │
                        └───────────────┬────────────────────┘
                                        │ DraftSignal
                        ┌───────────────▼────────────────────┐
                        │   RISK ENGINE (pure Python, veto)  │
                        └───────┬───────────────┬────────────┘
                            approved         rejected (logged)
                        ┌───────▼──────┐  ┌────▼─────┐
                        │ ALERT ROUTER │  │ AUDIT LOG │
                        │ (Telegram)   │  └──────────┘
                        └───────┬──────┘
                        ┌───────▼──────────────────────────┐
                        │ FastAPI ⇄ React web app           │
                        │ chat, dashboard, journal, signals │
                        └──────────────────────────────────┘
```

## 4. Agent Pipeline (LangGraph)

Implement as a typed StateGraph with a shared `PipelineState` (pydantic). Deterministic
computation happens in plain Python nodes; LLM calls are used only for interpretation,
synthesis, and explanation. Agents and responsibilities:

1. **Regime Agent** (deterministic + LLM summary): classifies market regime daily —
   bull-trend / bear-trend / range / high-volatility — using SPY vs 200-day SMA, 20-day
   realized vol percentile, VIX level/term proxy, ADX(14), and market breadth
   (advancers/decliners if available). Regime gates which strategies are eligible.
2. **Screener Agent** (deterministic): scans the configured watchlist + universe filters
   (min price, min average dollar volume, exchange listing) and emits candidate tickers
   with raw factor scores. No LLM.
3. **Analyst Agents** (run in parallel per candidate, each returns a structured
   pydantic verdict with score −100..+100, confidence 0..1, and evidence list):
   - **Technicals**: computes indicators in code (RSI-14, MACD, ATR-14, 20/50/200 SMA
     structure, VWAP, relative volume, 52-week high/low distance, support/resistance
     via swing highs/lows); LLM interprets the computed table only — it never invents
     numbers.
   - **News & Sentiment**: Haiku-tier triage of Finnhub company + market news; flags
     material events; outputs sentiment score with cited headlines.
   - **Fundamentals**: earnings date proximity, EPS/revenue surprise history, analyst
     revision direction, valuation vs sector (P/E, P/S percentile), Form 4 insider
     activity from EDGAR.
   - **Macro**: FRED-based context (rate direction, yield curve, CPI trend) mapped to
     sector sensitivities.
   - **Options Flow** (Phase 3 stub): interface defined now, implementation deferred —
     free-tier data is inadequate; document the paid upgrade path.
4. **Strategy Selector** (rules + LLM tie-break): given regime + analyst verdicts,
   selects one of {momentum-swing, mean-reversion, breakout, position-hold, cash}. Each
   strategy is a class with explicit entry/exit/stop logic and eligible-regime list.
   Cash/NO TRADE is always eligible.
5. **Portfolio & Sizing Agent** (deterministic): fixed-fractional sizing — risk at most
   `risk_per_trade_pct` (default 0.75%) of account equity per trade, stop distance =
   `atr_stop_multiple` (default 2.0) × ATR-14. Computes exact share count, max entry
   price (limit), stop loss, and take-profit at ≥ `min_reward_risk` (default 2.0) R:R.
   All parameters live in the user's risk profile, not code.
6. **Signal Synthesizer** (LLM): merges everything into the structured signal (schema
   below) with a plain-English explanation ≤ 500 chars and an evidence list referencing
   concrete data points. Confidence must be a calibrated aggregate, not vibes: weighted
   analyst agreement × regime fit × strategy historical hit-rate (from the evaluation
   store; neutral prior until 30+ resolved signals exist).
7. **Risk Engine** (pure Python, final gate — see §5).
8. **Performance Tracker** (deterministic, daily after close): resolves open signals
   against actual prices, updates hit-rates, Brier score for confidence calibration,
   per-strategy and per-regime expectancy, drawdown stats.
9. **Supervisor**: the LangGraph graph itself plus a watchdog that alerts (Telegram) on
   pipeline failures, stale data (>15 min during market hours), or provider outages.

### Signal schema (pydantic + DB table)

```python
class Signal(BaseModel):
    id: UUID
    created_at: datetime          # ET
    ticker: str
    action: Literal["BUY", "SELL", "HOLD", "NO_TRADE"]
    shares: int | None            # exact count; required for BUY/SELL
    max_entry_price: Decimal | None
    stop_loss: Decimal | None
    take_profit: Decimal | None
    confidence: float             # 0–1, calibrated
    expected_return_pct: float | None
    risk_score: int               # 1–10
    time_horizon: Literal["intraday","swing_days","position_weeks","long_term"]
    strategy: str
    regime: str
    evidence: list[EvidenceItem]  # source, datapoint, timestamp
    explanation: str
    risk_check: RiskCheckResult   # every rule evaluated, pass/fail, values
    alert_sent: bool
    user_decision: Literal["taken","skipped","modified","pending"] | None
```

## 5. Risk Engine (deterministic, absolute veto)

Location: `sentinel/risk/engine.py`. Pure functions, zero LLM calls, exhaustively unit
tested. Evaluates every DraftSignal against the active `RiskProfile` (DB-stored,
UI-editable, versioned). Rules — ALL must pass:

- `max_position_pct` of equity per position (default 10%)
- `max_open_positions` (default 8)
- `max_daily_loss_pct` — realized+unrealized today (default 2%); breach ⇒ system-wide
  BUY halt until next session
- `max_drawdown_pct` from equity high-water mark (default 10%); breach ⇒ BUY halt +
  alert
- `max_sector_pct` concentration (default 25%)
- `max_correlated_exposure` — sum of position weights with 90-day return correlation
  > 0.7 to the candidate (default 30%)
- `min_avg_dollar_volume` liquidity floor (default $5M/day) and position ≤ 1% of ADV
- `max_atr_pct` volatility filter (default: skip if ATR > 8% of price)
- `earnings_blackout_days` (default 2 trading days before earnings, configurable)
- `max_portfolio_exposure_pct` gross exposure cap (default 100%, no leverage)

Output is a `RiskCheckResult` listing every rule, the computed value, the limit, and
pass/fail. Rejections are stored and visible in the UI. There is no override code path
— do not build one.

## 6. Alerting

Telegram bot (python-telegram-bot). Send only when: action is BUY or SELL, confidence ≥
`alert_confidence_threshold` (default 0.80), and Risk Engine approved. Message format
(exact, no user math required):

```
🟢 BUY ALERT — NVDA
Shares: 18
Max Price: $875.20
Stop Loss: $842.10
Target: $910.00
Confidence: 93% | Risk: 4/10 | Horizon: swing (3–10d)
Expected: +4.8%
Why: institutional accumulation, bullish news flow, positive
earnings revisions, breakout above $868 on 2.1x volume
10:14 AM ET — Not financial advice. You place all trades.
```

SELL alerts must state exact shares to sell and realized P&L estimate. Also send:
daily pre-open brief (regime, watchlist status, open-position review) and daily
post-close recap. Rate-limit: configurable max alerts/day (default 5).

## 7. Web Application

React SPA served by FastAPI. Views:

1. **Chat** — conversational assistant (LiteLLM) with tool access to: latest signals,
   portfolio state, market context, performance stats, and on-demand single-ticker
   analysis ("Should I buy NVDA?" runs the pipeline for that ticker and returns the
   full signal + risk check). Chat can never bypass the risk engine or trigger alerts.
2. **Dashboard** — regime banner, indices, watchlist heat tiles, today's signals.
3. **Portfolio** — manual position entry (ticker, shares, cost basis), live valuation,
   exposure/sector/correlation views, drawdown chart.
4. **Signals feed** — live (WebSocket), full history, filterable; each signal expands
   to evidence + complete risk-check table; user marks taken/skipped/modified.
5. **Trade journal** — auto-created from signals + user decisions; free-text notes.
6. **Analytics** — hit rate, expectancy, Sharpe/Sortino (from resolved signals),
   confidence-calibration plot (predicted vs realized), performance by strategy and
   regime, missed-opportunity log.
7. **Settings** — risk profile editor (validated, versioned), model/provider picker,
   data-provider keys, alert threshold/quiet hours, watchlist manager.
8. **System** — logs, provider health, scheduler status, cost meter (LLM tokens + API
   calls per day).

Auth: single-user; local network by default; session login + HTTPS instructions for
any remote exposure; API keys server-side only.

## 8. Database Schema (Postgres + Timescale)

Tables (define with Alembic migrations): `bars` (hypertable: ticker, ts, ohlcv),
`quotes_latest`, `news_items`, `macro_series`, `fundamentals`, `earnings_calendar`,
`filings`, `regimes`, `signals`, `risk_checks`, `risk_profiles` (versioned),
`positions`, `trades` (user-entered fills), `journal_entries`, `alerts`,
`evaluations` (resolved-signal outcomes), `strategy_stats`, `chat_messages`,
`system_events`, `api_usage`.

## 9. Learning & Evaluation Loop

Nightly job resolves signals: a BUY signal is "resolved" when stop, target, or horizon
expiry is hit (tracked against actual bars — including signals the user skipped, so
missed opportunities and false positives are both measured). Update: per-strategy and
per-regime hit rate and expectancy (R multiples), confidence Brier score, drawdown
stats. These stats feed back into (a) the Signal Synthesizer's confidence calibration
and (b) Strategy Selector priors. Do NOT auto-tune risk limits — those change only via
explicit user edits in Settings.

## 10. Implementation Phases — build in this exact order

**Phase 0 — Skeleton (do first, verify it runs):** repo layout, Docker Compose
(postgres+timescale, redis, api, frontend, worker), config/pydantic-settings,
`.env.example`, Alembic baseline, health endpoints, CI with pytest + ruff.

**Phase 1 — Data layer:** provider interfaces + Alpaca/Finnhub/FRED/EDGAR
implementations, market-hours scheduler, ingestion jobs, rate-limit budgets in Redis,
data-staleness watchdog. Deliverable: dashboard-less API returning live market context.

**Phase 2 — Risk engine + portfolio:** risk profile model, all rules, exhaustive unit
tests (every rule: pass, fail, boundary), manual portfolio entry API. This lands BEFORE
any LLM agent so nothing ever ships unguarded.

**Phase 3 — Agent pipeline:** LangGraph graph, deterministic analysts first
(technicals/screener/regime), then LLM analysts (news, fundamentals, macro), strategy
selector, sizing, synthesizer. Golden-file tests with recorded fixtures; mock LLM in CI.

**Phase 4 — Alerts + signal lifecycle:** Telegram channel, thresholds, signal
persistence, user decision capture.

**Phase 5 — Web app:** all views above; WebSocket live feed.

**Phase 6 — Evaluation loop + paper-trading harness:** nightly resolution job,
analytics views, optional Alpaca paper account mirror for sanity-checking fills.

**Phase 7 — Hardening:** backup script, systemd/compose restart policies, log rotation,
security pass, production-readiness checklist in `docs/`.

After each phase: run tests, update `docs/PROGRESS.md`, and summarize what a user must
do (API keys, bot setup) in `docs/SETUP.md`.

## 11. Engineering Standards

- Type hints everywhere; pydantic models at every boundary; ruff + mypy clean.
- Every LLM call: JSON-schema-constrained output, retry with backoff, token/cost
  logging to `api_usage`, hard daily token budget with graceful degradation
  (skip LLM analysts, emit deterministic-only signals flagged as such).
- No LLM output is ever parsed into a trade parameter (shares, prices) — numeric trade
  parameters come only from deterministic code.
- Timezone discipline: store UTC, display/schedule in America/New_York; use an
  exchange-holiday calendar (`pandas_market_calendars`).
- Graceful degradation on provider outage: mark data stale, suppress signals, alert.
- Never commit secrets; add pre-commit hook with gitleaks.

## 12. Other

1.) Ask the user for: Alpaca + Finnhub + FRED + Anthropic API keys, Telegram bot token
   and chat ID, starting equity, and initial watchlist. Provide exact instructions for
   obtaining each.

Final principle: behave like a disciplined AI Chief Investment Officer. Correctness,
risk control, explainability, and auditability beat cleverness and trade frequency.
When uncertain, the answer is NO TRADE.
