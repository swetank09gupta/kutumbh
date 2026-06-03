# Kutumb — Architecture

## 1. Goals

Kutumb is a **self-hosted, email-driven personal finance dashboard** for a single household. Design goals, in priority order:

1. **Privacy by default.** No data leaves the user's machine. No third-party API gets to see account numbers, balances, or transactions.
2. **Zero manual data entry** for anything that arrives by email. Bank/broker statements are the system of record.
3. **Family-level aggregation.** Multiple owners (self, spouse, parents, children) roll up into a single net-worth view.
4. **One command to run.** `docker-compose up` and the dashboards are live.
5. **Auditable.** Every figure on a Grafana panel is traceable back to a raw email + a parsed row in Postgres.

Non-goals: real-time trading, multi-tenant SaaS, mobile app, plaid-style account scraping.

## 2. Component Overview

```
┌──────────────────┐      ┌─────────────────────────────────────────┐
│  IMAP mailboxes  │─────▶│   app/ingestion (email_fetcher,         │
│  Gmail · Yahoo   │      │   attachment_router, parsers/*)         │
└──────────────────┘      └────────────────┬────────────────────────┘
                                           │ normalized rows
                                           ▼
                          ┌─────────────────────────────────────────┐
                          │  app/etl  (normalizer, categorizer,     │
                          │           dedup)                        │
                          └────────────────┬────────────────────────┘
                                           ▼
                          ┌─────────────────────────────────────────┐
                          │  PostgreSQL 16  (system of record)      │
                          │  transactions · holdings · networth ·   │
                          │  manual_assets · goals                  │
                          └────────────────┬────────────────────────┘
                                           ▼
              ┌────────────────────────────┴────────────────────────────┐
              ▼                                                         ▼
  ┌────────────────────────┐                          ┌────────────────────────────┐
  │  app/engine            │                          │  Grafana 11                │
  │  networth · goals ·    │                          │  4 provisioned dashboards  │
  │  recommendations ·     │                          │  read-only Postgres DS     │
  │  portfolio_xirr        │                          └────────────────────────────┘
  └──────────┬─────────────┘
             ▼
  ┌────────────────────────┐
  │  FastAPI (app/api)     │
  │  /sync /accounts /     │
  │  /portfolio /goals /   │
  │  /manual /transactions │
  └────────────────────────┘
```

Five containers, all on one Docker network:

| Container | Image | Purpose |
|---|---|---|
| `finance_postgres` | postgres:16-alpine | System of record. Single source of truth. |
| `finance_app` | local Dockerfile | FastAPI + APScheduler + parsers. |
| `finance_ollama` | ollama/ollama | Local LLM fallback for unknown statement layouts. |
| `finance_ollama_init` | ollama/ollama | One-shot job: pulls the parse model on first boot. |
| `finance_grafana` | grafana/grafana:11.2.2 | Read-only visual layer. |

## 3. Data Flow

### 3.1 Ingestion (every 6h via APScheduler)

1. `email_fetcher` connects to each configured mailbox over IMAPS using **app passwords** (never the real login).
2. Each new message is checked against `email_seen(uid, mailbox)` — UIDs already processed are skipped.
3. `attachment_router` routes by `From:` + filename pattern to a parser in `app/ingestion/parsers/`.
4. Parser returns a list of canonical `Transaction` / `Holding` dicts.
5. If the parser fails (new layout, garbled OCR), the raw text is sent to `llm_extractor` which prompts the local Ollama model for a JSON extraction. Result is logged for human review and inserted with `source='llm'`.

### 3.2 ETL

- **Normalizer** rounds amounts to paise, applies `Asia/Kolkata` timezone, and stamps `owner_id`.
- **Categorizer** runs 50+ regex rules over `narration` → assigns `category` (salary, emi, sip, groceries, …) and `merchant`.
- **Dedup** uses `(account_id, posted_on, amount, narration_hash)` as the natural key. Re-running an import is idempotent.

### 3.3 Engines

| Engine | Output |
|---|---|
| `networth.py` | Daily snapshots per owner + family roll-up, written to `networth_snapshots`. |
| `holdings_view.py` | Current holdings, last price, market value (deduped per-ISIN across CDSL + broker mirrors). |
| `portfolio_xirr.py` | XIRR per instrument / asset class / owner / family. Per-ISIN bucketing for mixed accounts; synthesizes SGB and PPF purchase cashflows when statements don't expose them (see §3.6). |
| `goals.py` | Inflation-adjusted corpus target + SIP needed. Age-derived target dates for retirement (owner DOB + `retirement_age`) and child goals (child DOB + education/marriage milestone). |
| `holding_tagger.py` | Auto-tags holdings to goals from a per-ISIN map + fund-name regex + holding-type fallback. Runs on every sync and on a daily 00:15 IST scheduler sweep. |
| `ppf_from_transfers.py` | Synthesises PPF as a `ManualAsset` from bank-savings debits matching PPF-transfer patterns (Union Bank etc.). |
| `metal_prices.py` | Live gold/silver spot via Yahoo Finance, used to mark SGB holdings to market. |
| `recommendations.py` | Rule-based nudges (emergency-fund gap, EMI-to-income, asset allocation drift, PPF top-up). |

All engines are pure functions over the DB — re-running them is safe and idempotent.

### 3.6 Synthetic cashflows

Some holdings show up on a depository statement without a corresponding purchase transaction we can trace — typically SGBs (subscribed via a bank SGB application, not via the broker) and PPF (no e-statement at all). For these, the XIRR engine synthesises cashflows so the returns aren't either bogus (terminal-only inflow with no outflow) or trivially zero.

| Asset | Source of issue date | Source of cost basis | Source of current value |
|---|---|---|---|
| SGB (Sovereign Gold Bond) | RBI issuance calendar (`_SGB_TRANCHES` lookup); falls back to `maturity_year − 8 years` when ISIN unknown. | RBI tranche issue price per gram. | Live spot from `metal_prices.get_metal_prices()`. |
| PPF (via bank transfers) | Earliest detected transfer-to-PPF debit. | Sum of all detected transfer debits → individual `ManualAssetContribution` rows. | Compound the contributions at 7.1% quarterly to today. |

Both are owner-scoped (PPF lands in the owner whose bank account funds it; SGB inherits the broker account's owner) and bypassed entirely when a real statement-derived row exists for the same ISIN / institution.

### 3.7 Goal engine specifics

- **Age-derived target dates.** For `retirement` goals, `target_date = owner.birth_date + owner.retirement_age`. For `child_education` / `child_marriage` goals, `target_date = child.dob + education_age|marriage_age` from the optional `children:` block in `config/accounts.yml`. The stored `goal.target_date` is a fallback used only when the relevant profile data is missing.
- **Per-owner corpus.** Retirement corpus = owner's tagged-holdings sum + that owner's PPF. Earlier the family-aggregated PPF was added to every retirement goal — fixed in [ADR-0015](adr/0015-synthetic-cashflows.md).
- **Actual investing rate.** "On track" is computed against the family's real last-6-month investing rate (investment-account debits + ManualAssetContributions) distributed across investment goals in proportion to each goal's prescribed SIP. Previously the prescribed SIP itself was passed as the actual SIP, making every goal trivially on-track.

### 3.8 Caches

| Cache | Purpose | Location | Invalidation |
|---|---|---|---|
| `ParseResult` pickle | Skip pdfplumber on unchanged statements | `data/cache/parsed/` | `sha1(file_bytes)` changes, or `BaseParser._CACHE_SCHEMA` bump. |
| LLM response | Skip Ollama calls on identical prompt+input | `data/cache/llm/` | `sha1(model + prompt + input)` changes. |
| PDF text sidecar | `.txt.cache` next to each PDF for whole-doc-text parsers | next to source PDF | Source PDF mtime newer than sidecar. |

All caches honour an env-var off-switch (`PARSE_RESULT_CACHE=0`, `LLM_CACHE=0`, `PDF_TEXT_CACHE=0`). Numbers in [BENCHMARKS.md](BENCHMARKS.md) and [PERFORMANCE.md](PERFORMANCE.md).

### 3.4 API

FastAPI exposes everything under `/api/*`. Endpoints are read-mostly; the only mutating routes are `POST /api/sync/*` (trigger ingest) and `POST /api/manual/*` (add PPF/FD/NPS/real-estate that don't arrive by email).

### 3.5 Visualization

Grafana is provisioned at boot:
- Datasource `Postgres` wired to the `postgres` container with read-only role.
- Four JSON dashboards copied into `/var/lib/grafana/dashboards`.

The app and Grafana never talk to each other directly — Grafana only reads Postgres. This means dashboards keep working even if the FastAPI app is down.

## 4. Storage Model

Core tables (see `app/models/schemas.py`):

```
owners(id, name, role)                          -- self, spouse, child, parent
accounts(id, owner_id, institution, kind)       -- savings, demat, mf_folio, ppf, fd
transactions(id, account_id, posted_on,         -- bank txns
              amount, narration, category,
              merchant, source, hash)
holdings(account_id, instrument_id, qty,        -- demat / MF positions
         avg_cost, as_of)
networth_snapshots(owner_id, as_of, assets,     -- daily roll-up
                   liabilities, net)
manual_assets(id, owner_id, type, value,        -- PPF, NPS, gold, real estate
              interest_rate, updated_on)
goals(id, owner_id, name, target_amount,        -- "house 2030", "retire 2045"
      target_date, inflation_rate)
email_seen(mailbox, uid, processed_at)          -- ingestion idempotency
```

Postgres is the **only** persistent state. Ollama models are cached in a volume but are reproducible from the registry. Grafana state (dashboards, datasource) is provisioned from files — the `grafana_data` volume only holds user customisations.

## 5. Why This Shape

- **Postgres as system of record** instead of S3 + parquet: a household-scale dataset (~100k rows over a decade) fits in a single Postgres instance with room to spare; nothing benefits from a data lake here. See [ADR-0003](adr/0003-system-of-record.md).
- **Synchronous ingestion** (no Kafka, no Celery) because a 6-hour cadence over a few mailboxes does not need streaming. See [ADR-0004](adr/0004-no-message-queue.md).
- **Grafana over a bespoke React UI** because the user is the developer; visual exploration matters more than pixel-perfect UX, and Grafana ships templating + alerting for free. See [ADR-0010](adr/0010-grafana-as-frontend.md).
- **Local LLM (Ollama) instead of OpenAI** because statements contain salary, PAN, and account numbers — sending them to a hosted API would violate goal #1. See [ADR-0008](adr/0008-llm-locality.md).

## 6. Failure Modes & Recovery

| Failure | Effect | Recovery |
|---|---|---|
| New statement layout | Parser raises; row goes to LLM fallback. | LLM extracts; flagged `source='llm'` for review. Add a parser rule later. |
| IMAP rate-limit / lockout | Sync skipped. | Backoff in `email_fetcher`; manual `POST /api/sync/now` after cooldown. |
| Postgres data loss | Total loss of derived data. | Re-run `POST /api/sync/historical?since_days=...` — emails are the source of truth, the DB is rebuildable. |
| Grafana corruption | Dashboards gone. | Provisioning re-applies on container restart. |
| Wrong category assignment | One-off mis-labelling. | Add/adjust a regex in `categorizer.py`; re-run categorisation (idempotent). |

The whole system is **rebuildable from emails + config**. The DB is a cache.

## 7. Out of Scope

- No multi-tenancy. One household per deployment.
- No write-back to banks. Read-only.
- No mobile clients. Grafana mobile-web is the answer.
- No encryption at rest beyond what the host filesystem provides — physical-host trust is assumed.
