# 💰 Kutumb — Family Financial Dashboard

A self-hosted, private, email-driven personal finance tracker for the whole family.
One `docker-compose up` gives you a live Grafana dashboard fed by your bank & investment statements.

> **Docs:** [Architecture](docs/ARCHITECTURE.md) · [Decisions](docs/DECISIONS.md) · [ADRs](docs/adr/) · [Benchmarks](docs/BENCHMARKS.md) · [Performance](docs/PERFORMANCE.md) · [Experience](docs/EXPERIENCE.md) · [Production Readiness](docs/PRODUCTION_READINESS.md) · [Setup](SETUP.md) · [Submission](docs/SUBMISSION.md)

---

## What it does

| Feature | Detail |
|---|---|
| **Email ingestion** | Connects to Gmail + Yahoo (IMAP) every 6h, downloads statement attachments |
| **Bank parsers** | HDFC, SBI, Axis Bank, Union Bank of India — CSV & PDF |
| **Investment parsers** | Zerodha Kite (holdings + tradebook), CAMS/KFintech CAS MF PDFs (Coin) |
| **Smart categorisation** | 50+ rules for EMI, insurance, salary, groceries, SIPs, child expenses… |
| **Net worth tracking** | Daily snapshots per person + family combined |
| **Goal projections** | Inflation-adjusted corpus targets with SIP calculator |
| **Recommendations** | Emergency fund, goal shortfalls, asset allocation, EMI burden, PPF nudges |
| **Grafana dashboards** | 4 pre-built dashboards auto-wired to Postgres |
| **Manual entry API** | Add PPF, NPS, gold, real estate via REST API |
| **100% private** | Everything runs locally in Docker; private GitHub repo for code |

---

## Quick Start

### 1. Clone & configure

```bash
git clone https://github.com/YOUR_USERNAME/kutumb.git
cd kutumb

# Copy and fill in secrets
cp .env.example .env
nano .env

# Copy and fill in account config
cp config/accounts.example.yml config/accounts.yml
nano config/accounts.yml
```

### 2. Get email App Passwords (NOT your login password)

**Gmail:** myaccount.google.com → Security → 2-Step Verification → App passwords  
**Yahoo:** security.yahoo.com → Generate app password → Select "Mail"

### 3. Start everything

```bash
docker-compose up --build
```

- **App API:** http://localhost:8000
- **API docs:** http://localhost:8000/docs  
- **Grafana:** http://localhost:3000 (admin / `GRAFANA_PASSWORD` from .env)

### 4. Import historical data

On first run, trigger a 2-year historical import:

```bash
curl -X POST "http://localhost:8000/api/sync/historical?since_days=730"
```

Or trigger a regular sync:
```bash
curl -X POST "http://localhost:8000/api/sync/now"
```

---

## Setting Up Statement Emails

For each bank / broker, set up email statements to be sent to your registered email:

| Institution | Where to set up |
|---|---|
| **HDFC Bank** | NetBanking → My Account → e-Statement (monthly) |
| **SBI** | OnlineSBI → e-Statement → Register for e-statement |
| **Axis Bank** | NetBanking → My Profile → e-Statement |
| **Union Bank** | NetBanking → My Account → e-Statement |
| **Zerodha Kite** | Console → Profile → Enable Holdings + Tradebook email |
| **CAMS (Coin MF)** | camsonline.com → mailback → CAS PDF |
| **KFintech** | kfintech.com → CAS statement by email |

---

## Adding PPF / FD / NPS (Manual Assets)

PPF doesn't send email statements. Add them via the API:

```bash
# Add your PPF (SBI)
curl -X POST http://localhost:8000/api/manual/assets \
  -H "Content-Type: application/json" \
  -d '{
    "owner_id": "user",
    "asset_type": "ppf",
    "institution": "SBI",
    "name": "PPF Account",
    "current_value": 450000,
    "interest_rate": 7.1,
    "start_date": "2018-04-01"
  }'

# Add wife's PPF (Union Bank)
curl -X POST http://localhost:8000/api/manual/assets \
  -H "Content-Type: application/json" \
  -d '{
    "owner_id": "wife",
    "asset_type": "ppf",
    "institution": "Union Bank",
    "name": "PPF Account",
    "current_value": 320000,
    "interest_rate": 7.1
  }'
```

Update the value quarterly when you check your passbook.

---

## Grafana Dashboards

| Dashboard | What you see |
|---|---|
| **💰 Net Worth** | Family total, per-person, trend over time, asset allocation donut |
| **💸 Cash Flow** | Monthly income vs expenses, EMI tracker, category breakdown, top spends |
| **📈 Investments** | Full holdings table, P&L, portfolio allocation, value trend |
| **🎯 Goals & Planning** | Goal progress bars, SIP required, corpus gaps, family milestones |

Default login: `admin` / password from `.env` (`GRAFANA_PASSWORD`)

---

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/api/sync/now` | POST | Sync emails + compute net worth |
| `/api/sync/historical?since_days=730` | POST | Import 2 years of history |
| `/api/accounts/networth?owner_id=family` | GET | Net worth history |
| `/api/accounts/holdings` | GET | All current holdings |
| `/api/transactions?owner_id=user&category=emi` | GET | Filter transactions |
| `/api/transactions/summary` | GET | Monthly income/expense summary |
| `/api/goals/` | GET/POST | List / create goals |
| `/api/goals/projections` | GET | Corpus projections with SIP needed |
| `/api/goals/recommendations` | GET | Actionable recommendations |
| `/api/manual/assets` | GET/POST | PPF, FD, NPS manual entries |

Full interactive docs: http://localhost:8000/docs

---

## Privacy & Security

- **No cloud databases.** PostgreSQL runs inside Docker on your machine.
- **No third-party services.** Everything is self-hosted.
- **Private GitHub repo** keeps code private. `config/accounts.yml` and `.env` are git-ignored.
- **App passwords** (not your actual email password) are used for IMAP — can be revoked anytime.
- **No bank scraping** — uses only official email statements (safe, no risk of account lock).

---

## Running on a New System

```bash
# Pull the private repo
git clone https://github.com/YOUR_USERNAME/kutumb.git
cd kutumb

# Recreate .env and config/accounts.yml (not in git)
cp .env.example .env && nano .env
cp config/accounts.example.yml config/accounts.yml && nano config/accounts.yml

# Start
docker-compose up --build -d
```

Data is persisted in Docker named volumes (`postgres_data`, `grafana_data`).
To move data between machines, export/import the postgres volume.

---

## Project Structure

```
kutumb/
├── app/
│   ├── ingestion/         # Email fetcher + statement parsers
│   │   └── parsers/       # HDFC, SBI, Axis, Union Bank, Zerodha, CAS MF
│   ├── etl/               # Normaliser, categoriser, dedup
│   ├── engine/            # Net worth, goals, recommendations
│   ├── api/routes/        # FastAPI endpoints
│   ├── models/            # SQLAlchemy ORM schemas
│   ├── scheduler.py       # APScheduler background jobs
│   └── main.py            # FastAPI app + startup
├── grafana/
│   ├── provisioning/      # Auto-wired datasource + dashboards
│   └── dashboards/        # 4 pre-built JSON dashboards
├── config/
│   └── accounts.example.yml
├── docker-compose.yml
└── .env.example
```
