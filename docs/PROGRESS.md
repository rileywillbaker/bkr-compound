# Build Progress

Phase status per `claude-code-master-prompt.md` §10.

| Phase | Scope | Status |
|---|---|---|
| 0 | Skeleton: repo, compose, config, Alembic, health, CI | **done** (container run deferred until Docker Desktop installed) |
| 1 | Data layer: providers, ingestion, scheduler, watchdog | **done** (live-key verification deferred until keys pasted) |
| 2 | Risk engine + portfolio | **done** |
| 3 | Agent pipeline (LangGraph) | **done** (options-flow analyst is a stub per spec — paid data upgrade path documented in its docstring) |
| 4 | Alerts + signal lifecycle | pending |
| 5 | Web app | pending |
| 6 | Evaluation loop + paper harness | pending |
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
