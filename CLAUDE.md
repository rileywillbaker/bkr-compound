# B-Quant — project conventions

Spec: `claude-code-master-prompt.md` (authoritative). Plan: see docs/PROGRESS.md for phase status.

## Hard rules (from spec — never violate)
- The system NEVER executes trades. No live order endpoints, ever.
- Risk engine (`sentinel/risk/`) is pure Python, no LLM, absolute veto, **no override code path**.
- No LLM output is ever parsed into a trade parameter (shares/prices come only from deterministic code).
- LLM models are never hardcoded — resolve roles via `config/models.yaml` (LiteLLM).
- Store UTC in DB; schedule/display in America/New_York.
- Disclaimers on every user-facing surface and alert.

## Cost rules (2026-07 optimization pass — see docs/COST_MODEL.md)
- **Claude is the last step in the pipeline, never the first.** If deterministic
  Python can answer it, do not call an LLM. Adding a per-candidate LLM call to
  any stage that runs over the universe is the one change to never make.
- The LLM stage runs ONLY on candidates that already passed every deterministic
  filter **and** the risk pre-filter, have an event trigger, and fit under the
  operating mode's hard per-scan cap.
- One combined call per finalist (`sentinel/agents/review.py`) — not a
  per-analyst fan-out. The fan-out exists only at "full" depth (user-initiated
  research / Research mode).
- Reviews are cached by a fingerprint of the material facts; an unchanged
  situation must never be re-analyzed at cost.
- LLM output in the review is a **categorical stance**, never a number. The
  stance→multiplier mapping lives in code and is one-directional: it may hold
  or lower conviction, never raise it, and `reject` downgrades to NO_TRADE.
- Free mode must remain structurally incapable of making a call.

## Toolchain on this machine (not on PATH — use full paths)
- Python venv: `.venv\Scripts\python.exe` (Python 3.12.10, per-user install at `%LOCALAPPDATA%\Programs\Python\Python312`)
- Git: `C:\Users\riley\tools\git\cmd\git.exe`
- Node/npm: `C:\Users\riley\tools\node\node.exe`, `C:\Users\riley\tools\node\npm.cmd`
- Docker: NOT INSTALLED — user must install Docker Desktop (see docs/SETUP.md §1)

## Commands
- Tests: `.venv\Scripts\python.exe -m pytest`
- Lint: `.venv\Scripts\python.exe -m ruff check sentinel tests`
- Types: `.venv\Scripts\python.exe -m mypy sentinel`
- Stack: `docker compose up --build` (once Docker exists)

## Layout
`sentinel/` Python package (providers/, data/, risk/, portfolio/, agents/, pipeline/, strategies/, swing/, evaluation/, alerts/, scheduler/, api/, db/) · `frontend/` React+Vite+Tailwind SPA · `alembic/` migrations · `tests/` pytest.

Cost-critical modules: `sentinel/modes.py` (operating modes) · `sentinel/data/cache.py` (TTL cache) · `sentinel/agents/review.py` (the one paid call) · `sentinel/pipeline/triggers.py` (event gating) · `sentinel/pipeline/graph.py` (the funnel).

Universe lists are `config/universe_*.csv` — globbed and unioned, so adding a file needs no code change.
