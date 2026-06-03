# Production Readiness

"Production" for Kutumb means **a single household running this on a home server or NAS, with the dashboards trusted enough to make real money decisions**. Not "SaaS with 99.99% SLA". This document tracks how close we are to that bar and what's still missing.

## Status legend
- ✅ Done
- 🟡 Partial / good enough for now
- ❌ Not done

## 1. Correctness

| Item | Status | Notes |
|---|---|---|
| Deterministic parser unit tests (HDFC/SBI/Axis/Union/Zerodha/CAMS) | 🟡 | Fixtures committed for 4/6; SBI + Union still ad-hoc. |
| XIRR engine validated against Excel `XIRR()` | ✅ | 12 fixtures, ±1e-4. |
| Categoriser regression tests | 🟡 | ~30 cases; need to cover refunds and reversals. |
| Idempotent re-ingest (`POST /sync/historical`) | ✅ | Verified by re-running over the same window — zero net DB change. |
| Timezone correctness (IST end-of-month) | ✅ | `TZ` + `PGTZ` set on every container. |
| Floating-point money | ✅ | Stored as `paise` (integer); display layer divides by 100. |

## 2. Reliability

| Item | Status | Notes |
|---|---|---|
| IMAP transient failure handling | ✅ | Backoff + skip; resumes next 6h tick. |
| Parser failure does not poison the batch | ✅ | One bad attachment is isolated; the rest of the batch commits. |
| LLM fallback timeout | ✅ | 120s hard cap; failures logged with `source='llm_failed'`. |
| Postgres healthcheck gate on app startup | ✅ | `depends_on: service_healthy`. |
| Scheduler missed-tick handling | 🟡 | APScheduler default coalesce=True; an outage >6h merges into one run. Documented, not tunable yet. |
| Crash recovery / restart policy | 🟡 | Compose has implicit restart on `app`; not set to `unless-stopped`. Easy fix. |

## 3. Security

| Item | Status | Notes |
|---|---|---|
| No outbound calls to third-party APIs | ✅ | Verified by container egress policy in deployment guide. |
| App passwords, not real passwords, in `.env` | ✅ | Documented in README + `.env.example`. |
| `.env` and `config/accounts.yml` git-ignored | ✅ | Tested via `git check-ignore`. |
| Grafana admin password forced via env | ✅ | No default `admin/admin`. |
| Postgres not exposed to host | ✅ | No `ports:` mapping on the `postgres` service. |
| Anonymous Grafana viewer role | 🟡 | Enabled for convenience on LAN. Document the trade-off; disable if exposing beyond LAN. |
| Secrets encryption at rest | ❌ | Relies on host disk encryption (FileVault / LUKS). Out of scope for a household deployment. |
| HTTPS / reverse proxy | ❌ | Out of the box this is HTTP-on-LAN. Tailscale or Caddy in front is the user's responsibility — sample Caddyfile in `deploy/`. |

## 4. Observability

| Item | Status | Notes |
|---|---|---|
| Grafana "Parser health" panel (unknown category %, llm_source %) | ✅ | Leading indicator of upstream breakage. |
| FastAPI access logs | ✅ | Default uvicorn JSON logs to stdout. |
| Postgres slow-query log | ❌ | Not configured; not needed at current scale. |
| Per-sync run log table | 🟡 | `email_seen` records UIDs but not run-level metadata (start, end, n_parsed, n_failed). Want to add `sync_runs` table. |
| Alerting | 🟡 | Grafana alerting available but no default alerts shipped. Suggest: "no sync ran in 24h" + "parser_failed > 0 today". |

## 5. Data lifecycle

| Item | Status | Notes |
|---|---|---|
| Backup strategy documented | ✅ | `pg_dump` cron + raw `./data/raw` rsync — covered in `deploy/BACKUP.md`. |
| Rebuild-from-emails verified | ✅ | Drop DB, `POST /sync/historical?since_days=3650`, dashboards rebuild identically. |
| Schema migrations | ✅ | Alembic, autogenerate gated by review. |
| PII handling on deletion | 🟡 | `DELETE` on `transactions` cascades to derived tables but raw attachments in `./data/raw` are independent — document the manual step. |

## 6. Performance

See `BENCHMARKS.md`. Summary:

- All dashboard panels p95 <60ms over 9 years of household data.
- Cold historical sync ~6–9 minutes for 5 mailboxes / 2 years.
- LLM fallback worst-case ~35s/statement on CPU; only triggers for new layouts.

No performance work is required for the target deployment size. Documented anyway because "fits in Postgres" surprises people.

## 7. Known gaps (the honest list)

1. **No automated end-to-end test.** There's no `make e2e` that boots the stack, drops a fixture mailbox, runs sync, and asserts on dashboard SQL. Parser tests exist; integration tests don't.
2. **No `sync_runs` audit table.** I can tell *what* ingested but not *when* the last attempt happened or how long it took. Coming.
3. **Recommendations engine is rules-only.** Good enough for the four scenarios shipped (emergency fund, EMI ratio, allocation drift, PPF top-up). A fifth rule will demand a small DSL.
4. **No multi-currency.** Everything is INR. Adding USD/EUR would need a `currency` column on every monetary table and a daily FX feed.
5. **Coin per-instrument XIRR has a gap.** Coin emails lack ISIN; AMFI lookup table integration would close it. Tracked in code as a TODO.
6. **CDSL demat ≈ broker mirror, partial cost basis.** The CDSL eCAS statement is a position-only snapshot of the same equity that the Zerodha demat broker view also shows. Only Zerodha-side contract notes give us cost basis; pre-archived buys (older than the e-mailed contract-note window) carry no recorded cost. The `demat` class is excluded from total XIRR with reason "no cost basis". Closing this needs older contract notes ingested or a one-time manual cost-basis import.
7. **PPF "synth from transfers" missing pre-window balance.** When PPF is reconstructed from bank-savings transfer debits, the opening balance prior to the earliest detected transfer is not captured. The synthetic corpus under-states reality if the PPF account is older than the bank-statement window. Fix path: opt-in `ppf_opening_balance` + `ppf_opening_date` in `accounts.yml`, OR ingest the annual PPF statement when a parser exists.
8. **Stale account stubs.** Some parsers create placeholder `Account` rows with `account_ref='unknown'` and never link real txns. Cleanable with a single SQL prune (`delete from accounts where (txns,hldgs,bals)=(0,0,0)`); not yet automated.

## 8. Definition of "ready" for this project

A household deployment is ready when:

- All four Grafana dashboards render with current data.
- A `POST /api/sync/historical?since_days=730` round-trip completes cleanly.
- The "Parser health" panel shows `unknown_category_pct < 5%` and `llm_source_pct < 5%`.
- A `pg_dump` backup has been taken in the last 7 days.

That bar is met today on the reference deployment.
