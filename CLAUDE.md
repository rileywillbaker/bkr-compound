# B-Quant — project conventions

Spec: `claude-code-master-prompt.md` (authoritative). Plan: see docs/PROGRESS.md for phase status.

## Hard rules (from spec — never violate)
- The system NEVER executes trades. No live order endpoints, ever.
- Risk engine (`sentinel/risk/`) is pure Python, no LLM, absolute veto, **no override code path**.
- No LLM output is ever parsed into a trade parameter (shares/prices come only from deterministic code).
- LLM models are never hardcoded — resolve roles via `config/models.yaml` (LiteLLM).
- Store UTC in DB; schedule/display in America/New_York.
- Disclaimers on every user-facing surface and alert.

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
`sentinel/` Python package (providers/, data/, risk/, portfolio/, agents/, pipeline/, strategies/, evaluation/, alerts/, scheduler/, api/, db/) · `frontend/` React+Vite+Tailwind SPA · `alembic/` migrations · `tests/` pytest.
