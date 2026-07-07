# Build Progress

Phase status per `claude-code-master-prompt.md` §10.

| Phase | Scope | Status |
|---|---|---|
| 0 | Skeleton: repo, compose, config, Alembic, health, CI | **done** (container run deferred until Docker Desktop installed) |
| 1 | Data layer: providers, ingestion, scheduler, watchdog | **done** (live-key verification deferred until keys pasted) |
| 2 | Risk engine + portfolio | pending |
| 3 | Agent pipeline (LangGraph) | pending |
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
