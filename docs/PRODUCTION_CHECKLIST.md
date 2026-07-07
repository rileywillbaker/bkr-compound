# B-Quant Production Readiness Checklist

Work through this before treating the system as always-on. Every item is
verifiable in minutes.

> Reminder: outputs are informational only, not financial advice. B-Quant
> never executes trades — there is no order endpoint in the codebase.

## Secrets & auth

- [ ] `.env` exists, is NOT committed (`git status` clean of it), and holds a
      unique `APP_SECRET_KEY` (48+ random chars).
- [ ] `APP_PASSWORD` is strong and unique; `APP_ENV=prod` so the session
      login is enforced (all `/api` routes 401 until login).
- [ ] `POSTGRES_PASSWORD` changed from the default.
- [ ] Provider keys entered via Settings → Providers (encrypted at rest),
      not left in `.env` on shared machines.
- [ ] Pre-commit hooks installed (`pre-commit install`) so gitleaks scans
      every commit.

## Network exposure

- [ ] All compose ports bind to `127.0.0.1` (they do by default — verify no
      one edited them to `0.0.0.0`).
- [ ] Remote access, if any, goes through a TLS reverse proxy (Caddy/nginx)
      or a VPN/Tailscale — never plain HTTP over the internet.
- [ ] API rate limit + security headers active (built-in; verify with
      `curl -i localhost:8000/api/settings` → `X-Content-Type-Options`).

## Data & backups

- [ ] Daily `scripts/backup.ps1` (Windows Task Scheduler) or
      `scripts/backup.sh` (cron) scheduled; `backups/` has fresh dumps.
- [ ] A restore has been TESTED once against a scratch database
      (`pg_restore --clean --if-exists`).
- [ ] Docker volume `db_data` lives on a disk with free space monitoring.

## Services & recovery

- [ ] `docker compose ps` shows db/redis/api/worker all healthy with
      `restart: unless-stopped`.
- [ ] Log rotation confirmed (compose `x-logging`: 10 MB × 5 files/service).
- [ ] Docker Desktop set to start on login (Settings → General) so the stack
      survives reboots.
- [ ] `/health/deep` returns `ok` for database and redis.

## Alerting sanity

- [ ] `POST /api/providers/telegram/test` delivers a message to your phone.
- [ ] Alert threshold, max/day cap, and quiet hours reviewed in Settings.
- [ ] Ops alerts verified: stop the worker container for >15 min during
      market hours → staleness alert arrives.

## Costs & budgets

- [ ] `LLM_DAILY_TOKEN_BUDGET` sized to your comfort (System view shows a
      live 7-day cost meter).
- [ ] Anthropic console has a monthly spend limit configured server-side.

## Risk discipline (spec invariants — verify, never "fix")

- [ ] Risk profile reviewed in Settings; every change creates a new version.
- [ ] There is no override path for risk rejections anywhere in UI or API
      (`tests/test_risk_engine.py::test_no_override_code_path_exists` guards
      this — keep it green).
- [ ] Signals with `deterministic_only=true` are treated as lower-trust
      (they mean the LLM budget/keys were unavailable).
