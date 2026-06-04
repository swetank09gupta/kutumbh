# Key Decisions

A reading-order summary of the trade-offs. For full context per decision, see `docs/adr/`.

| # | Decision | Chose | Rejected | Why |
|---|---|---|---|---|
| 1 | Language / runtime | Python 3.12 + FastAPI | Go, Node, Java | PDF/email parsing ecosystem (pdfplumber, mailparser, pandas) is unmatched. Personal tool — iteration speed > raw throughput. |
| 2 | DB | Postgres 16 | SQLite, DuckDB, MongoDB | Household data fits in a single Postgres; gives us JSONB, window funcs, and a Grafana datasource that just works. |
| 3 | System of record | Postgres | S3 + parquet, raw mailbox | Need ad-hoc SQL for dashboards. Mailbox is the *source*; Postgres is the *materialized* SoR. |
| 4 | Message queue | None | Kafka, Redis Streams, Celery | 6h cadence × ~5 mailboxes = 0 events/sec. APScheduler in-process is enough. |
| 5 | Blob storage | Local filesystem (`./data/raw`) | S3, MinIO | Privacy goal: no external services. Volume mount is fine for tens of MB/month. |
| 6 | Multi-tenancy | None — single household | Row-level security, per-owner schemas | Out of scope. `owner_id` column distinguishes family members. |
| 7 | Auth | None on local network; basic auth at reverse proxy if exposed | OIDC, JWT | Lives behind LAN / Tailscale. Building OIDC for a single-user tool is overkill. |
| 8 | LLM provider | Local Ollama (llama3.2:1b default) | OpenAI, Claude API | Statements contain PAN, salary, account numbers. Hosted APIs are a non-starter. |
| 9 | LLM usage | Fallback only — parsers first, LLM if parser fails | LLM-first extraction | Deterministic parsers are cheaper, faster, and auditable. LLM only covers the long tail of new layouts. |
| 10 | Frontend | Grafana provisioned dashboards | Custom React/Next app | User is the developer; templating + alerting for free; saves weeks. |
| 11 | Categorisation | Regex rules in `categorizer.py` | ML classifier | 50 rules cover >95% of rows; rules are debuggable in 30s, models aren't. |
| 12 | Idempotency | Natural key dedup `(account, date, amount, hash(narration))` | Insert + on-conflict update with surrogate UUIDs | Re-running historical import must be a no-op even if attachments shift order. |
| 13 | Scheduling | APScheduler in-process | Cron, k8s CronJob, Celery beat | Single container, single process — no orchestration to manage. |
| 14 | Deployment | docker-compose on a home server | k8s, bare metal, cloud | Five containers, one host, one user. Compose is the right altitude. |
| 15 | Observability | Postgres logs + FastAPI access logs + Grafana itself | OpenTelemetry, Prometheus stack | For a personal tool, dashboards on the data are more useful than dashboards on the dashboards. |
| 16 | XIRR engine | Custom Newton-Raphson over cashflows | numpy-financial, scipy.optimize | Avoids a heavy dep; matches Excel XIRR to 6 decimals on test cashflows. |
| 17 | Manual assets API | Plain REST writes | Spreadsheet import | PPF/FD update quarterly — a one-line curl is faster than maintaining a CSV. |
| 18 | Untraceable cost basis | Synthesize cashflows on the fly (SGB tranche table, PPF transfer detection) | Persist fake transactions; require manual cost entry | Keeps the rebuild-from-emails invariant intact while still letting XIRR include real returns. See [ADR-0015](adr/0015-synthetic-cashflows.md). |
| 19 | Goal target dates | Derive from owner DOB + `retirement_age` / child DOB + milestone | Trust the user's static `target_date` | Static dates rot the moment the user's life changes. Derivation keeps SIP honest without manual edits. See [ADR-0016](adr/0016-age-based-goal-dates.md). |
| 20 | Goal projection model | Auto-blended XIRR (capped) + step-up + glide path, with per-goal manual override | Flat single-rate projection per goal | Lets the projection track real holdings without manual tuning, while keeping defaults conservative for goals with no tagged data. See [ADR-0019](adr/0019-goal-projection-model.md). |
| 21 | SIP detection | Cadence (≥3 hits) + amount-stability (CV < 15%) + ₹100 snap + bank-ACH mandate sweep; report per-txn average as current monthly rate | Sum debits ÷ months | Lumpsums and discretionary buys don't masquerade as SIPs; recently-started SIPs aren't under-counted; owners with un-ingested broker mail still get bank-mandate SIPs detected. See [ADR-0020](adr/0020-sip-detection.md). |

Each row above is expanded in its own ADR under `docs/adr/`.
