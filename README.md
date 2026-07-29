# B-Quant

Personal AI-agentic stock analysis and trade-recommendation platform.

> **Disclaimer:** All outputs are informational only, not financial advice.
> Past performance does not guarantee future results. This system **never
> executes trades** — you place all trades manually and are solely responsible
> for them.

## Core guarantees

1. **No trade execution.** No brokerage order endpoint is ever called with a
   live order. Alpaca is used for market data (and optional paper simulation).
2. **NO TRADE is a first-class output.** Most days the correct output is nothing.
3. **Deterministic Risk Engine with absolute veto.** Pure Python, no LLM, no
   override code path.
4. **Full auditability.** Every input, agent output, risk check, and user
   decision is logged with timestamps.
5. **Unlimited deterministic computation, minimal AI inference.** Claude is the
   last step in the pipeline, never the first. Hundreds of stocks are screened
   on every scan for $0; at most a handful of finalists — names that already
   cleared every filter *and* the risk engine — are worth a single cached LLM
   call each.

## Operating modes

Set in **Settings → Operating mode**. The mode governs automatic AI spend only.
Screening, technicals, discovery, sizing, the risk engine, portfolio
management, alerts and briefs run identically in all three.

| Mode | Scheduled scans | When you ask directly | Target cost |
|---|---|---|---|
| **Free** | no AI at all | no AI at all | **$0/month** |
| **Smart** (default) | ≤ 3 finalists, one call each | full multi-agent analysis | 1–5 analyses/day |
| **Research** | full multi-agent analysis | full multi-agent analysis | several × Smart |

`GET /api/system/budget` reports live spend against every cap. See
[`docs/COST_MODEL.md`](docs/COST_MODEL.md) for the full funnel, caching
strategy, event triggers, and backstops.

## Quick start

```bash
cp .env.example .env      # fill in APP_SECRET_KEY + APP_PASSWORD at minimum
docker compose up --build
# open http://localhost:8000 — the onboarding wizard walks you through API keys
```

See `docs/SETUP.md` for step-by-step instructions, including how to obtain
every API key (all free tiers).

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check sentinel tests
mypy sentinel
# frontend dev server with hot reload:
docker compose --profile dev up frontend
```

## Architecture

Scheduler → Data layer (TimescaleDB + Redis) → LangGraph agent pipeline →
**Risk Engine (veto)** → Alerts (Telegram) + Audit log → FastAPI ⇄ React UI.

The pipeline is a multi-stage filtering funnel, cheapest filters first:

```
Universe (600+)  →  technical  →  liquidity  →  fundamental/quality  →
deterministic analysts  →  strategy fit  →  sizing  →  RISK ENGINE (pre-filter)
→  event gate + hard cap  →  LLM review  →  synthesis  →  RISK GATE (final)
```

Everything above the LLM row costs CPU and nothing else, which is why the
universe (S&P 500 ∪ Nasdaq-100 ∪ liquid large caps, editable via
`config/universe_*.csv`) can be wide. The risk engine runs twice: once as an
economic gate so tokens are never spent on a trade that cannot happen, and once
as the final authority on every signal — that one has no override path.

Alongside the signal pipeline, a deterministic **portfolio manager** reviews
every open position on each scan (hold / trim / tighten stop / take partial
profits / add / exit), with any exposure-increasing suggestion cleared by the
risk engine first.

See `claude-code-master-prompt.md` for the full specification,
`docs/COST_MODEL.md` for the cost architecture, and `docs/PROGRESS.md` for
build status.
