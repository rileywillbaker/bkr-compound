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

Scheduler → Data layer (TimescaleDB + Redis) → LangGraph agent pipeline
(Regime → Screener → Analysts ∥ → Strategy → Sizing → Synthesizer) →
**Risk Engine (veto)** → Alerts (Telegram) + Audit log → FastAPI ⇄ React UI.

See `claude-code-master-prompt.md` for the full specification and
`docs/PROGRESS.md` for build status.
