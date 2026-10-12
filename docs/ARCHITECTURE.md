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

### 3.1 Ingestion (every 6h via APScheduler; one pipeline, `app/etl/pipeline.py`: statements, bank alerts, loan letters, public rules, rebuild)

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
| `recommendations.py` | Rule-based nudges. Originally: emergency-fund gap, EMI-to-income, asset allocation drift, PPF top-up. Extended (see §3.10) with portfolio-quality checks (midcap overweight, international gap, scattered direct equity, ELSS 80C dead-weight) and market-aware investment-timing recs (where + how much + when, given current Nifty P/E zones). |
| `market_signals.py` | Live Nifty 50 / Midcap 150 / Smallcap 250 / Nifty 500 P/Es from NSE daily-close CSV (`archives.nseindia.com`), USD/INR + Nasdaq 100 from Yahoo. 12-hour cached. Zones (`cheap`/`fair`/`expensive`) computed vs 5-yr rolling averages; midcap premium % over Nifty 50 derived. Drives the timing language in market-aware recs. |
| `recommendation_memory.py` | Persists rec snapshots (`recommendation_snapshots` table) once per (fingerprint, date). Diffs today vs the most-recent snapshot 7-35 days ago to surface 🆕 new, ✅ resolved, 🔄 persisting buckets. Numeric metrics (e.g. `midcap_pct`) are snapshotted so the UI shows trend arrows over months. |

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
- **Per-goal effective return.** A goal's projection uses a *blended* return: the value-weighted XIRR of holdings + manual assets tagged to that goal, capped at a long-run category benchmark (equity 13.5%, MF 13%, FD 7%, gold 9%, …) so a fund that ran 28% in a 3-yr bull market doesn't extrapolate at 28% for 30 years. If the blended figure is implausibly low (< 4%, e.g. fresh holdings sitting at a paper loss) the engine falls back to the conservative default. The user can override with a manual `expected_return_rate` per goal. Implementation lives in `app/engine/goal_returns.compute_blended_return`. See [ADR-0019](adr/0019-goal-projection-model.md).
- **SIP step-up.** Per-goal `sip_stepup_pct` lets the projected SIP grow annually. If left blank, the engine auto-detects from the owner's last 24 months of investing activity (ratio of months [-11..0] / [-23..-12]), clamped to 0–12%. The math compounds the step-up each completed 12 months inside the FV-of-annuity calculation.
- **Glide path.** For long-horizon goals, the return tapers linearly from the start-rate to `glide_end_return_pct` (default 8%) over the last `glide_start_years_before` years (default 3). Implemented as a per-month return schedule (`_monthly_return_schedule`); the same schedule drives both lump-sum and SIP future-value computations, and the SIP-needed solver does a binary search across it.
- **Actual investing rate.** "On track" is computed against the family's real recent investing — but only *recurring SIPs* count, not lumpsums. A broker-account flow qualifies as a SIP when it has ≥3 hits in the lookback window AND its per-txn amounts have CV < 15% (i.e. amounts are tight). The rate reported is the per-txn average (current monthly SIP rate), not `total / months` — so SIPs that started recently aren't under-counted. SIP amounts are snapped to the nearest ₹100 to absorb NAV-rounding paisa. SIPs that ride on the **bank statement** (NACH mandates to Zerodha/Coin at `YESB00709000028661`) are also detected by description-match, so an owner whose Coin emails aren't ingested still contributes to the goal SIP totals. See [ADR-0020](adr/0020-sip-detection.md).
- **Per-goal attribution.** Actual SIPs are attributed to a specific goal when the underlying instrument's `goal_tag` matches the goal's `goal_type` (resolved via ISIN → Holding for broker transactions, or directly on ManualAsset for manual contributions). Any un-attributable family-level SIP is spread across goals in proportion to `monthly_sip_needed` so nothing is silently lost.

### 3.9 Recommendation engine — checks

All checks are pure functions of the DB + (optionally) `market_signals`. Each rec carries a `priority`, `category`, `title`, `detail`, `action`, and an optional `metrics` dict (key numbers that drove the rec — snapshotted for trend display).

| Check | Fires when | Key metric |
|---|---|---|
| `_emergency_fund_check` | Liquid savings < 6× monthly expenses | `liquid_months` |
| `_goal_shortfall_check` | Projection < inflation-adjusted target | `gap` |
| `_high_expense_check` | Any non-EMI category > 30% of monthly expenses | `share` |
| `_asset_allocation_check` | Equity < 40% or Debt > 60% of net worth | `equity_pct`, `debt_pct` |
| `_emi_burden_check` | Recurring monthly EMI > 40% of monthly income. EMI is detected from `category=emi` debits via amount-grouping (≥3 hits, σ/μ < 10%) so one-off prepayments and statement duplicates don't inflate the burden. | `emi_ratio` |
| `_ppf_nudge` | PPF contributions this FY < ₹1.5 L | `ppf_contrib` |
| `_80c_dead_weight_check` (per owner) | 80C consumed ≥ ₹1.5 L cap **AND** ELSS holdings exist. Consumed = PPF (this FY) + EPF × 12 + owner's share of home-loan principal. Principal uses real amortisation when loan params exist on the liability row (EMI − outstanding × rate / 12), else falls back to 30% of total `emi` category. | `total_80c_consumed`, `elss_value` |
| `_midcap_concentration_check` | Mid/small-cap funds ≥ 25% of equity book | `midcap_pct` |
| `_international_gap_check` | International equity < 5% of equity book (equity ≥ ₹5 L) | `intl_pct` |
| `_direct_equity_fragmentation_check` | > 5 distinct stocks, avg position < ₹1.5 L | (positions, avg) |
| `_retirement_target_sanity_check` | Combined household retirement target < 70% of (annual lifestyle expense × 25) | (target_sum, rule_of_thumb) |
| `_market_timing_recommendations` | Per-gap timing advice. Drops specific fund shortlists (curated quarterly) + timing ("lump-sum now" / "standard SIP" / "STP over 6-12 mo" / "wait") based on Nifty 50 / Midcap 150 zones from `market_signals`. Includes a "Lock In G-Sec Yields" sub-rec when India 10-yr ≥ 7.25% and the household lacks ≥ ₹2 L in long-duration debt MFs. | varies |

### 3.10 Recommendation memory & change detection

Every call to `/api/goals/recommendations` snapshots the active recs into `recommendation_snapshots` (idempotent: one row per `(owner_id, fingerprint, snapshot_date)` — repeated dashboard loads on the same day update in place, they don't duplicate). `fingerprint = "{category}|{title}"` is stable across time even as detail/action wording drifts, so lifecycle is trackable.

`/api/goals/recommendations/changes` diffs today's set vs the most recent snapshot 7-35 days ago and returns:

- **`new`** — fingerprints active today, absent in prior snapshot.
- **`resolved`** — active in prior snapshot, absent today. Usually means the user took the recommended action.
- **`persisting`** — active in both. Each carries `since_date`, `count_snapshots`, and a `trend` of the primary numeric metric over the lookback window so the UI can show "midcap_pct: 26.1 → 23.4" with a coloured arrow.

This solves the "noise vs signal" problem the user raised: recs are stable from one month to the next (they drift in ₹ amounts but the title/category persists), and the persisting bucket makes it visually obvious which advice has been ignored long enough to act on.

### 3.11 Co-borrower / household tax model

Three new owner fields drive personalised 80C and EMI-burden logic:

- `tax_regime` (`old` | `new`) — only `old` is checked for ELSS dead-weight; under `new` regime no 80C is claimable.
- `epf_monthly` — employee EPF contribution per month. Annualised to consume 80C.
- `home_loan_share_pct` — co-borrower share of the household home loan. Sum across owners (e.g. user 33 + wife 34 = 67%) is < 100% when there's an external co-borrower (e.g. parent); that gap is handled in the EMI burden calc.

Liability rows (`ManualAsset.asset_type = "liability"`) gained a `monthly_payment` column. Combined with `interest_rate` and `current_value` (negative = outstanding principal), the 80C check can compute the exact principal portion of each EMI as `EMI − outstanding × rate / 1200`, instead of the 30% rule-of-thumb. Prepayments beyond the regular EMI are treated as 100% principal.

### 3.12 Caches

| Cache | Purpose | Location | Invalidation |
|---|---|---|---|
| `ParseResult` pickle | Skip pdfplumber on unchanged statements | `data/cache/parsed/` | `sha1(file_bytes)` changes, or `BaseParser._CACHE_SCHEMA` bump. |
| LLM response | Skip Ollama calls on identical prompt+input | `data/cache/llm/` | `sha1(model + prompt + input)` changes. |
| PDF text sidecar | `.txt.cache` next to each PDF for whole-doc-text parsers | next to source PDF | Source PDF mtime newer than sidecar. |
| Market signals | Live Nifty P/Es + USD/INR + Nasdaq levels for the rec engine | `data/market_signals_cache.json` | 12 hours since `cached_at`. |
| India CPI | World Bank latest-inflation rate for goal projections | `data/inflation_cache.json` | 24 hours. |
| Metal prices | COMEX gold/silver + USD/INR conversion for SGB MTM | `data/metal_prices_cache.json` | 6 hours. |

All caches honour an env-var off-switch (`PARSE_RESULT_CACHE=0`, `LLM_CACHE=0`, `PDF_TEXT_CACHE=0`). Numbers in [BENCHMARKS.md](BENCHMARKS.md) and [PERFORMANCE.md](PERFORMANCE.md).

### 3.4 API

FastAPI exposes everything under `/api/*`. Endpoints are read-mostly; the only mutating routes are `POST /api/sync/*` (trigger ingest) and `POST /api/manual/*` (add PPF/FD/NPS/real-estate that don't arrive by email).

### 3.5 Visualization

Grafana is provisioned at boot:
- Datasource `Postgres` wired to the `postgres` container with read-only role.
- Four JSON dashboards copied into `/var/lib/grafana/dashboards`.

The app and Grafana never talk to each other directly — Grafana only reads Postgres. This means dashboards keep working even if the FastAPI app is down.

Alongside Grafana, `app/static/index.html` is a single-file vanilla-JS SPA served at `/ui/` for the views that don't fit a dashboard (CRUD modals, sync trigger, per-goal projection drill-downs, scenario panel). The SPA:
- Uses **hash routing** (`#overview`, `#investments`, `#goals`, …) so refresh, back/forward, and bookmarks all land on the active tab. State is held in `location.hash` + `history.pushState`; no client-side router framework.
- Lazy-loads Grafana iframes per tab — each iframe starts with `src=""` and is populated by `refreshGrafana(section)` only when its tab is visited.
- Shows **skeleton loaders** (shimmer placeholders) instead of "Loading…" text for tables/cards/lists during async fetches.
- Modal dismiss is wired uniformly (Esc + click-outside) across all four modals (goal / asset / profile / contribution / scenarios).
- Page width is fully fluid — no hard `max-width` cap; tables and panels fill the available viewport on any monitor.

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
manual_assets(id, owner_id, type, value,        -- PPF, NPS, gold, real estate, liability
              interest_rate, monthly_payment,    -- monthly_payment = EMI on liability rows
              updated_on)
goals(id, owner_id, name, target_amount,        -- "house 2030", "retire 2045"
      target_date; inflation is one app-wide setting, ADR-0019)
email_seen(mailbox, uid, processed_at)          -- ingestion idempotency

-- Owner tax profile (drives 80C dead-weight & EMI burden splitting; UI-editable)
owners(id, name, birth_date, risk_appetite, retirement_age,
       tax_regime,         -- "old" | "new"
       epf_monthly,        -- employee EPF share
       home_loan_share_pct)-- co-borrower share of household home loan

-- Recommendation memory (one row per fingerprint per day; idempotent)
recommendation_snapshots(id, snapshot_date, owner_id,
                          fingerprint,        -- "category|title", stable across days
                          priority, category, title, detail, action,
                          metrics)            -- JSON of driving numbers (midcap_pct, etc.)
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
