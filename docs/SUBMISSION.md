# Submission Notes

A short reader's guide to this repo for someone landing here cold.

## What this is

**Kutumb** — a self-hosted personal-finance dashboard for a household. Emails in, Grafana out. Five Docker containers, one `docker-compose up`, no third-party services.

The detailed pitch is in [README.md](../README.md). This file is the meta — how the repo is organised and how to read it efficiently.

## Suggested reading order

1. **[README.md](../README.md)** — what it does, quick start, API surface. 5 min.
2. **[docs/ARCHITECTURE.md](ARCHITECTURE.md)** — components, data flow, storage model, failure modes. 10 min.
3. **[docs/DECISIONS.md](DECISIONS.md)** — the trade-off table; one line per choice. 3 min.
4. **[docs/adr/](adr/)** — full ADRs if you want the *why* behind any row in DECISIONS.md.
5. **[docs/EXPERIENCE.md](EXPERIENCE.md)** — what went wrong while building it. The honest log.
6. **[docs/BENCHMARKS.md](BENCHMARKS.md)** — numbers. Real workload, measured, not hand-waved.
7. **[docs/PRODUCTION_READINESS.md](PRODUCTION_READINESS.md)** — what's done, what's partial, what's missing.

If you only have 10 minutes: README → ARCHITECTURE → skim DECISIONS.

## What to look at in the code

| If you care about… | Start here |
|---|---|
| How email statements become rows | [app/ingestion/email_fetcher.py](../app/ingestion/email_fetcher.py) → [app/ingestion/attachment_router.py](../app/ingestion/attachment_router.py) → `app/ingestion/parsers/*.py` |
| The trickiest parser | [app/ingestion/parsers/hdfc.py](../app/ingestion/parsers/hdfc.py) — Layout A/B/C, headerless-page reconstruction |
| How rows become dashboards | [app/etl/normalizer.py](../app/etl/normalizer.py) → [app/etl/categorizer.py](../app/etl/categorizer.py) → [app/engine/networth.py](../app/engine/networth.py) |
| The XIRR engine | [app/engine/portfolio_xirr.py](../app/engine/portfolio_xirr.py) |
| How Grafana sees the data | [grafana/provisioning/](../grafana/provisioning/) + [grafana/dashboards/](../grafana/dashboards/) |
| The DB schema | [app/models/schemas.py](../app/models/schemas.py) + [alembic/versions/](../alembic/versions/) |

## Design themes worth calling out

1. **The DB is a cache; emails are the source of truth.** Drop Postgres and `POST /sync/historical` rebuilds everything. This shaped the dedup design (natural keys, not UUIDs) and the LLM fallback (auditable, not authoritative).
2. **Parsers before LLM, always.** The LLM only sees what the parsers can't. Cheaper, faster, debuggable — and means most of the system has no AI dependency at all.
3. **Grafana over a custom UI.** This saved weeks. It also forced clean SQL semantics on every engine, because every panel is a query.
4. **Idempotency everywhere.** Re-running ingest, re-running categorisation, re-running net-worth snapshots — all no-ops on unchanged input.

## What this repo is *not*

- Not a SaaS. No multi-tenancy, no auth, no rate limits.
- Not a trading platform. Read-only.
- Not finished. See [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) §7 for the honest gap list.

## How to verify it works on your machine

```bash
cp .env.example .env          # fill in IMAP app passwords
cp config/accounts.example.yml config/accounts.yml
docker-compose up --build -d
curl -X POST "http://localhost:8000/api/sync/historical?since_days=730"
open http://localhost:3000    # Grafana — admin / GRAFANA_PASSWORD
```

If the four dashboards render with data inside 10 minutes, the system is working as designed.
