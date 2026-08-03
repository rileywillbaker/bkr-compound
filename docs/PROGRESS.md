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
| 7 | Hardening | **done** |

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
- 2026-07-07 — Phase 7 landed: compose log rotation (10MB×5 per service) +
  configurable POSTGRES_PASSWORD; pg_dump backup scripts (Windows + Linux,
  14-dump retention, restore reference); API rate limit (240 req/min/client
  on /api) + security headers middleware; docs/PRODUCTION_CHECKLIST.md;
  SETUP.md backup section. Remaining: final end-to-end verification needs
  Docker Desktop installed by the user (containers, hypertables, live keys).
- 2026-07-24 — User feedback: the pre-open brief (regime + a plain watchlist
  price list) wasn't actionable — it wouldn't have caught MU's drift from a
  $1,200 52-week high down to ~$840, since B-Quant's existing discovery
  triggers (unusual_volume, macro_move) only look at single-day anomalies,
  never a gradual multi-week drawdown. Added: (1) a `pullback_from_high`
  discovery trigger using fundamentals already in the DB — zero new keys —
  plus an `elevated_short_interest` trigger; (2) three new optional provider
  integrations following the existing ABC/registry/credentials pattern —
  Yahoo (`OverviewProvider`, keyless, fills fundamentals gaps; unofficial
  endpoint, degrades gracefully), Finviz Elite (`ScreenerProvider`, one
  export call screens the *whole* market for a 52-week-high pullback, not
  just the static ~500-name universe — the main new "discover it early"
  capability; requires a paid Elite subscription), Fintel (`InstitutionalDataProvider`,
  short interest / dark-pool; requires a paid API plan, and Fintel's public
  docs don't fully enumerate endpoints logged-out, so treat it like the
  options-flow stub — wired per spec, live-verified once a real key is
  pasted); (3) `short_interest` table + migration 0008, `ingest_short_interest`
  scoped to the day's scan set (not full-universe, since Fintel's rate limits
  aren't published); (4) pre-open brief now leads with an "Opportunities"
  section (why a symbol was flagged) instead of just prices. Settings →
  Providers gained Finviz/Fintel cards (Yahoo needs no card — always on).
  226 tests green, ruff/mypy clean, frontend build clean.
- 2026-07-29 — Cost-optimization pass. Goal: operating cost as close to $0 as
  possible without weakening recommendation quality or the risk architecture.
  **The single biggest change**: the pipeline used to spend ~7 LLM calls per
  screened candidate (five analysts + strategy tie-break + synthesis narrative)
  on every one of the three daily core scans and both daily swing scans. It now
  spends ONE cached call per *finalist* — a name that already passed every
  deterministic filter AND the risk engine. Details:
  (1) `sentinel/modes.py` — three operating modes (free / smart / research)
  stored in app_settings; mode governs automatic spend only and never touches
  the risk engine, sizing, or any deterministic output. Free mode is
  structurally incapable of making a call.
  (2) `sentinel/agents/review.py` — the single combined review call. Returns a
  categorical stance from a fixed enum plus prose; code owns the stance→number
  mapping and it is one-directional (confirm 1.0 / caution 0.85 / reject →
  NO_TRADE). A review can only make the system more conservative. The
  synthesizer's dedicated explanation call was deleted entirely.
  (3) `sentinel/pipeline/graph.py` restructured into an explicit funnel with a
  `risk_prefilter` node BEFORE the LLM stage — purely economic, so tokens are
  never spent on a trade the engine would veto. The terminal `risk_gate` is
  unchanged and remains the only authority.
  (4) `sentinel/pipeline/triggers.py` — event-based gating (earnings, filings,
  news, breakout, pullback, volume, insider cluster, momentum, open position,
  high conviction, user request) plus a hard per-scan cap, so a chaotic market
  day cannot blow the budget.
  (5) `sentinel/data/cache.py` + migration 0010 (`cache_entries`) — TTLs for
  provider data that barely changes (fundamentals 7d, profile 30d, earnings
  calendar 20h, short interest 3d, filing summaries 90d) and fingerprinted LLM
  reviews so an unchanged situation is never re-analyzed at cost. Nightly purge.
  (6) Universe expanded from ~500 to 600+: `config/universe_*.csv` are now
  globbed and unioned (S&P 500 + Nasdaq-100 + liquid large caps). Cost-neutral
  because every full-universe stage is arithmetic over stored bars. Screener
  gained quality/liquidity floors (cap ≥ $2B, sector known, 200+ bars) that
  exclude penny/OTC/micro-cap/thin-data names, plus a relative-strength factor.
  (7) Ingestion tiered: bars/macro/calendar cover the full universe; the
  expensive per-symbol pulls (news, filings, insiders, fundamentals) target a
  deterministic focus set (~60, `technical_focus_set`) ∪ candidates ∪ watchlist
  ∪ holdings. `full_universe_deep_ingest` restores the old sweep.
  (8) `sentinel/portfolio/manager.py` — deterministic portfolio manager (EXIT /
  REDUCE / TAKE_PARTIAL_PROFITS / TIGHTEN_STOP / INCREASE / HOLD / NO_ACTION).
  Runs in every mode including Free; any exposure-increasing suggestion is
  cleared by the risk engine first. Exits become SELL signals through the normal
  persistence/gate/alert path — previously nothing generated SELLs at all.
  (9) Discovery gained relative_strength, breakout, uptrend_pullback,
  sector_leadership and earnings_revision triggers (all from stored bars +
  earnings calendar, zero new keys) and a quality gate that fails OPEN on
  missing data.
  (10) LLM client gained a daily call-count backstop and `budget_status()`;
  models.yaml gained a `review` role (Sonnet — one good judgement beats five
  cheap ones when you only make three a day) and dropped the daily cap to $0.25.
  (11) API: `/api/settings/modes`, `PUT /api/settings/mode`,
  `PUT /api/settings/ingest-scope`, `GET /api/portfolio/review`,
  `POST /api/pipeline/research`, `GET /api/system/budget`. Frontend: operating
  mode selector, position-management panel, live AI-budget panel. New chat tool
  `portfolio_review`.
  Docs: `docs/COST_MODEL.md`, plus a Free-mode note in SETUP.md (the Anthropic
  key is now optional). 321 tests green (was 226), ruff/mypy clean, frontend
  build clean. Unchanged: no trade execution, risk engine purity and absolute
  veto, no LLM output in any trade parameter, NO TRADE as a first-class outcome.
  Deployment note: migration 0010 must run and the image be rebuilt.
- 2026-08-02 — **Trend Discovery Agent** (`sentinel/trends/`, migration 0011,
  docs/TREND_DISCOVERY.md). Finds emerging themes early from **free, keyless
  sources only** and hands survivors to the existing agents rather than acting
  on them. Fourteen themes in `taxonomy.py` (nuclear, uranium, AI, AI
  regulation, robotics, defense, cybersecurity, semiconductors, clean energy,
  power grid, infrastructure, quantum, space, critical minerals); adding one
  needs no code change anywhere else.
  (1) Sources: publisher RSS (Yahoo/CNBC/MarketWatch/Seeking Alpha/Investing/
  Nasdaq) + per-theme Google News RSS search (the workhorse, and the only free
  route to Reuters since it retired its feeds); Federal Register API and
  USAspending API (both documented, free, no key — the best free policy signal
  available) plus DoD/DOE/NRC/White House feeds; Reddit public `.json` and
  StockTwits free endpoints. **X/Twitter returns nothing by design** — no free
  read tier exists, and the report states the gap rather than faking or
  scraping it.
  (2) ETF activity has two signals: a flow proxy from our *own* bars (thematic
  ETF relative strength + dollar-volume trend — always available, needs no
  external source; `config/thematic_etfs.csv`, deliberately not matching the
  `universe_*.csv` glob so ETFs never become stock picks), plus day-over-day
  **diffs** of issuer-published holdings where free (ARK/Global X/iShares). An
  empty diff means "no free holdings data", never "no accumulation".
  (3) `sentiment.py` — offline finance-tuned lexicon (VADER grammar handling +
  Loughran-McDonald vocabulary, implemented natively). No torch dependency, no
  paid API, fully deterministic. ALL-CAPS emphasis ignores tickers/acronyms.
  (4) Scoring: six weighted components where **action outweighs talk**
  (market+policy 45 > news+social 32), plus an explicit hype guard that CAPS
  the score (one-directional) when it finds the hype signature — social
  dominating with no confirmation, one stock carrying the basket, narrow
  breadth. A source that fails is *not covered* and leaves both numerator and
  denominator, so a dead feed never reads as an absence of news.
  (5) Ranking: quality gate (≥$5, ≥$10M turnover, ≥$500M cap, ≥120 bars, known
  sector — fails CLOSED since this output carries a dollar amount) and a
  pump-and-dump guard requiring the full signature *and* no earnings/8-K to
  explain the move. Seven factors, with valuation ranked **within the theme's
  peers** (an absolute PE cutoff would exclude every growth theme) and
  competitive advantage labelled as the proxy it is.
  (6) Agent hand-off: `trend_alignment` discovery trigger puts thematic names
  into the normal scan set (scored below the event triggers — a theme is a
  reason to look, not to buy); `allocation.py` converts a ranked idea to
  dollars via `size_position()` → cash cap → the real risk engine → portfolio
  context. Cash is capped in allocation, not as a new risk-engine rule, so the
  pure module is untouched and the cap only ever shrinks a proposal.
  (7) Cost: everything above is $0. The only paid step is one review call per
  **theme** (never per ticker), ≤2/day in Smart, ≤4 in Research, **0 in Free**,
  cached and one-directional. Free mode produces the complete report.
  (8) Jobs at 07:45 ET (collect+score, before the 08:30 pre-market job so its
  discovery pass sees fresh snapshots) and 09:50 ET (report). New `trends`
  role in models.yaml, `/api/trends/*` router, Trends tab.
  (9) `agent._with_proposed()` makes the recommendation basket **cumulative**:
  each candidate is risk-checked against a portfolio already containing the
  ones above it, so five names cannot each pass in isolation and together
  breach the sector / correlation / gross-exposure limits. Verified: with a
  25% sector cap the third same-sector name is refused on `max_sector_pct`.
  132 trend tests (454 total, was 322), ruff/mypy clean, frontend build clean.
  Deployment note: migration 0011 must run and the image be rebuilt.
- 2026-07-07 — Pre-Docker smoke test: booted the real app (SQLite, TestClient)
  serving the real built SPA — 14/14 checks passed (health, SPA, auth,
  settings, encrypted+masked credentials, risk profile, manual trade +
  valuation, analytics, clean empty pipeline scan with no data, WebSocket
  feed, disclaimers). Still pending for full sign-off (user action first):
  1) install Docker Desktop → `docker compose up --build` → verify
     migrations/hypertables/health; 2) paste real API keys in onboarding →
     Test buttons green → live ingest; 3) live Telegram test message.
