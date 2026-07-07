# B-Quant Setup Guide

> All outputs are informational only, not financial advice. B-Quant never
> executes trades.

## 1. Prerequisites

### Docker Desktop (required to run the stack)

Windows 11:

1. Download from <https://www.docker.com/products/docker-desktop/>.
2. Run the installer **as administrator**. Keep "Use WSL 2" checked.
3. If prompted, allow it to enable WSL 2 / Virtual Machine Platform and reboot.
4. Start Docker Desktop and wait until the whale icon reports "running".
5. Verify in a terminal: `docker compose version`

### Optional local dev tools

Only needed if you want to run tests or the frontend outside Docker:
Python 3.12+, Node 20+, Git.

## 2. Bootstrap configuration

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set at minimum:

- `APP_SECRET_KEY` — generate with
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`
- `APP_PASSWORD` — your web login password

Everything else can be configured later in the web UI.

## 3. Start the stack

```powershell
docker compose up --build -d
```

Open <http://localhost:8000>. On first run the **onboarding wizard** walks you
through starting equity, watchlist, risk profile, and API keys. Each key has a
"Test connection" button. Keys are stored encrypted server-side and never
shown again in full.

## 4. Getting the API keys (all free)

### Alpaca (market data — IEX feed)

1. Go to <https://alpaca.markets> → **Sign up** (choose the free account).
2. In the dashboard, switch to **Paper Trading** (toggle top-left).
3. Open **Home → API Keys** (right side) → **Generate New Keys**.
4. Copy the **API Key ID** and **Secret Key** into B-Quant.

B-Quant uses Alpaca for market data and optional paper-trade mirroring only.
It never places live orders.

### Finnhub (news, fundamentals, earnings calendar)

1. Go to <https://finnhub.io> → **Get free API key**.
2. Register and confirm your email.
3. Your API key is shown on the dashboard at <https://finnhub.io/dashboard>.

Free tier: 60 calls/minute. B-Quant rate-limits itself accordingly. Some
premium endpoints are unavailable on the free tier — B-Quant degrades
gracefully and marks those factors "unavailable".

### FRED (macro data: rates, CPI, yield curve, VIX)

1. Go to <https://fred.stlouisfed.org> → **My Account → Create Account**.
2. Once logged in: **My Account → API Keys** →
   <https://fred.stlouisfed.org/docs/api/api_key.html> → **Request API Key**.
3. Describe the use ("personal research") and copy the 32-character key.

### Anthropic (LLM analysis)

1. Go to <https://console.anthropic.com> and create an account.
2. Add a small amount of billing credit (Settings → Billing). Typical usage is
   cents per day; B-Quant enforces a hard daily token budget.
3. **Settings → API Keys → Create Key**, copy the `sk-ant-...` value.

### Telegram (alerts)

1. In Telegram, open **@BotFather** → send `/newbot` → follow prompts.
   Copy the **bot token** (looks like `123456789:AA...`).
2. Get your **chat id**: open **@userinfobot** and press Start — it replies
   with your numeric id.
3. **Important:** send your new bot any message (e.g. "hi") so it is allowed
   to message you back.
4. Paste both values into B-Quant and press "Test" — you should receive a
   test message.

### SEC EDGAR (filings — no key needed)

The SEC requires a descriptive User-Agent containing contact info. Set
`EDGAR_USER_AGENT` in `.env` (or the Settings UI) to something like
`B-Quant/0.1 (you@example.com)`.

## 5. Login & remote access

- On your local network (default), no login is required (`APP_ENV=dev`).
- To require the session login, set `APP_ENV=prod` and a strong
  `APP_PASSWORD` in `.env`, then restart the stack. All API routes then
  return 401 until you sign in through the web login page.
- **Never expose the app to the internet over plain HTTP.** For remote
  access, put a TLS reverse proxy in front (Caddy is the simplest:
  `caddy reverse-proxy --from your.domain.com --to localhost:8000`), or use
  a VPN/Tailscale to reach your home network instead of exposing a port.
- API keys never leave the server; the browser only ever sees masked
  previews.

## 6. Development workflow

```powershell
# Python
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\pytest
.venv\Scripts\ruff check sentinel tests
.venv\Scripts\mypy sentinel

# Frontend hot reload (Vite dev server on :5173, proxying to the api)
docker compose --profile dev up frontend
```

## 7. Backups

Daily database dumps with 14-dump retention:

```powershell
# Windows: run once to test, then schedule daily
powershell -NoProfile -File scripts\backup.ps1
schtasks /Create /SC DAILY /ST 23:30 /TN "B-Quant backup" `
  /TR "powershell -NoProfile -File $PWD\scripts\backup.ps1"
```

On a Linux VPS use `scripts/backup.sh` via cron. Test a restore once —
see the restore command at the bottom of either script. Before going
always-on, walk through `docs/PRODUCTION_CHECKLIST.md`.

## 8. Troubleshooting

- **API container unhealthy** — `docker compose logs api`. Most common cause:
  missing `.env` or a migration failure.
- **No data flowing** — check Settings → Providers; every provider shows its
  last successful call and any error. Also see the System view for the
  staleness watchdog.
- **No Telegram alerts** — alerts fire only for risk-approved BUY/SELL signals
  above your confidence threshold and below the daily alert cap. Check the
  Signals view: a signal shown there but not alerted will display the reason.
