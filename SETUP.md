# Setup

A step-by-step guide for getting Kutumb running on a fresh machine.

## Prerequisites

- Docker Desktop (macOS / Windows) or Docker Engine + Compose v2 (Linux)
- ~3 GB free RAM (or ~700 MB if you disable the Ollama LLM fallback)
- ~5 GB free disk
- An email account (Gmail or Yahoo) that receives your bank statements

## 1. Clone

```bash
git clone <your-fork-url> kutumb
cd kutumb
```

## 2. Configure secrets

```bash
cp .env.example .env
```

Edit `.env` and set:

| Variable | What |
|---|---|
| `POSTGRES_PASSWORD` | Any strong string |
| `GRAFANA_PASSWORD` | Grafana admin password |
| `GMAIL_USER` / `GMAIL_APP_PASSWORD` | Gmail app password (see step 3) |
| `YAHOO_USER` / `YAHOO_APP_PASSWORD` | Yahoo app password (if used) |
| `OLLAMA_PARSE_MODEL` | `llama3.2:1b` (default, fast on CPU) or `llama3.2` (3b, better, slower) |

## 3. Get email app passwords

**Never** put your actual Gmail/Yahoo login password in `.env`. Use an app password:

- **Gmail:** [myaccount.google.com](https://myaccount.google.com) → Security → 2-Step Verification → App passwords → "Mail" → copy the 16-char string.
- **Yahoo:** [security.yahoo.com](https://security.yahoo.com) → Generate app password → "Mail" → copy.

App passwords can be revoked at any time without touching your main account.

## 4. Configure accounts

```bash
cp config/accounts.example.yml config/accounts.yml
```

Edit `config/accounts.yml` to list:
- Each owner in the household (`self`, `spouse`, `parent`, `child`)
- Each bank / broker account, mapped to an owner

Comments in the example file walk you through each field.

## 5. Start the stack

```bash
docker-compose up --build -d
```

First boot:
- Postgres initialises (~10s)
- Alembic migrations run automatically
- `ollama-init` pulls the LLM model (~700 MB for `llama3.2:1b`, one-time, ~2 min)
- Grafana provisions datasource + 4 dashboards

Check everything is up:

```bash
docker-compose ps
curl http://localhost:8000/health
```

## 6. First historical import

```bash
curl -X POST "http://localhost:8000/api/sync/historical?since_days=730"
```

This walks back 2 years of statements. Expect 6–9 minutes for 5 mailboxes. Watch progress:

```bash
docker-compose logs -f app
```

## 7. Open the dashboards

- **API docs (Swagger):** http://localhost:8000/docs
- **Grafana:** http://localhost:3000 — login `admin` / `$GRAFANA_PASSWORD`

The four dashboards (Net Worth, Cash Flow, Investments, Goals) should now show data.

## 8. (Optional) Add manual assets

PPF, FD, NPS, gold, real estate don't arrive by email. Add them via API:

```bash
curl -X POST http://localhost:8000/api/manual/assets \
  -H "Content-Type: application/json" \
  -d '{
    "owner_id": "self",
    "asset_type": "ppf",
    "institution": "SBI",
    "name": "PPF",
    "current_value": 450000,
    "interest_rate": 7.1,
    "start_date": "2018-04-01"
  }'
```

Re-run quarterly when you check passbooks.

## 9. (Optional) Expose beyond LAN

By default the stack listens on `localhost` only. If you want to reach it from your phone:

- **Easy + secure:** install [Tailscale](https://tailscale.com) on the host and your phone. Reach it at the host's tailnet hostname.
- **Self-managed:** put Caddy in front for HTTPS + basic auth. A sample `Caddyfile` is in [`deploy/`](deploy/).

**Do not** expose port 8000 or 3000 directly to the internet without auth in front.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ollama-init` keeps restarting | Check `docker-compose logs ollama` — needs ~1.5 GB free RAM to load the model. |
| `app` container exits with `DATABASE_URL` error | Make sure `POSTGRES_USER`/`POSTGRES_PASSWORD` in `.env` are non-empty. |
| Grafana shows "No data" | Run `POST /api/sync/historical?since_days=730` first. |
| Dashboards show wrong month | Confirm `TZ=Asia/Kolkata` is set on both `postgres` and `app` in `docker-compose.yml`. |
| New bank statement isn't being parsed | Check the "Parser health" Grafana panel for `source='llm'` rows or unknown layouts. |

## Backup

```bash
# Postgres dump
docker exec finance_postgres pg_dump -U financeuser financedb \
  | gzip > backups/finance-$(date +%F).sql.gz

# Raw attachments
rsync -av ./data/raw/ /path/to/backup/disk/finance-raw/
```

Schedule both nightly via host cron. Restore: drop the volume, `gunzip < backup.sql.gz | docker exec -i finance_postgres psql -U financeuser financedb`, then `POST /sync/historical` to re-derive anything that drifted.
