# Build Progress

Phase status per `claude-code-master-prompt.md` §10.

| Phase | Scope | Status |
|---|---|---|
| 0 | Skeleton: repo, compose, config, Alembic, health, CI | **done** (container run deferred until Docker Desktop installed) |
| 1 | Data layer: providers, ingestion, scheduler, watchdog | **done** (live-key verification deferred until keys pasted) |
| 2 | Risk engine + portfolio | **done** |
| 3 | Agent pipeline (LangGraph) | **done** (options-flow analyst is a stub per spec — paid data upgrade path documented in its docstring) |
| 4 | Alerts + signal lifecycle | **done** (live Telegram send untested until the user pastes bot credentials — use `POST /api/alerts/test`) |
| 5 | Web app | **done** (Analytics outcome stats show empty state until Phase 6 resolves signals) |
| 6 | Evaluation loop + paper harness | **done** (fill sanity-checking is the deterministic bar-walk simulator; no brokerage order endpoints exist, live or paper) |
| 7 | Hardening | pending |

## Notes

- 2026-07-06 — Phase 0 started. Dev machine had no toolchain; installed
  per-user Python 3.12.10, MinGit 2.55, Node 24 LTS. **Docker Desktop must be
  installed by the user** (admin + WSL2 required) — see SETUP.md §1.
  Container verification deferred until then; unit tests run in local venv.
- Model IDs `claude-sonnet-4-6` / `claude-haiku-4-5` verified against the
  current Anthropic catalog. VIX sourced via FRED `VIXCLS` (Alpaca free tier
  has no index data).
- 2026-07-07 — Phase 2 landed: risk engine (all §5 rules, exhaustive tests),
  fixed-fractional sizing (capped by max_position_pct pre-veto), manual
  portfolio entry API, migration 0003.
- 2026-07-07 — Phase 3 landed: LLM client (role routing, budget,
  schema-validated JSON), five analysts with deterministic fallbacks, five
  strategies + selector (rules; LLM breaks ties by *name* only), typed
  LangGraph pipeline ending in the risk gate, synthesizer with deterministic
  confidence (neutral hit-rate prior until Phase 6), `POST /api/pipeline/run`.
  End-to-end tests run with mocked LLM (happy path, earnings-blackout veto,
  LLM-outage degradation). Signal DB persistence + alerts are Phase 4.
- 2026-07-07 — Phase 4 landed: signals/risk_checks/alerts/journal_entries
  tables (migration 0004), signal persistence in the runner, user decision
  capture (auto journal entries), Telegram channel with spec §6 message
  format, alert router (BUY/SELL + approved + confidence ≥ 0.80, max 5/day),
  pre-open/post-close briefs on the existing scheduler hooks, ops alerts for
  watchdog/pipeline failures, `/api/signals` + `/api/alerts` routers.
  Telegram is mocked in tests; live send needs bot_token/chat_id pasted in
  Settings (validate via `POST /api/providers/telegram/test`).
- 2026-07-07 — Phase 5 landed: chat assistant (read-only tools; single-ticker
  analysis runs the real pipeline through the risk gate), `/ws` live feed
  (Redis pub/sub relay + in-process bus), session auth (enforced when
  APP_ENV=prod), app_settings store (watchlist/equity/quiet-hours/onboarding,
  migration 0005), quiet-hours alert suppression, analytics summary endpoint,
  and the full React SPA: onboarding wizard with per-provider signup
  instructions + paste-key + Test buttons, Dashboard, Chat, Signals (live,
  expandable risk-check table, taken/skipped/modified), Portfolio, Journal,
  Analytics, Settings (versioned risk-profile editor, watchlist, keys),
  System (provider health, cost meter, event log). Disclaimer on every
  surface. `npm run build` clean; 172 tests green; ruff/mypy clean.
- 2026-07-07 — Phase 6 landed: evaluations + strategy_stats tables (migration
  0006), nightly resolution (02:00 ET job) walking daily bars with a
  pessimistic stop-first tie-break; skipped signals resolve too (missed-
  opportunity log); Brier score + calibration buckets + Sharpe/Sortino;
  per-strategy/per-regime hit-rate & expectancy rollups; synthesizer now
  reads real blended hit-rate priors (neutral until 30+ resolved, spec §9);
  analytics endpoint + Analytics view show calibration plot and strategy
  table. Spec's "optional Alpaca paper mirror" intentionally satisfied by
  the internal bar-walk fill simulator instead — CLAUDE.md's hard rule is
  no order endpoints of any kind. Risk limits are never auto-tuned.
