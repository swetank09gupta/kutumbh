# ADR-0015: Synthesize cashflows for holdings without traced purchases

**Status:** Accepted
**Date:** 2026-06

## Context

Two common Indian household holdings show up on a depository / position statement without any matching purchase transaction we can ingest:

1. **Sovereign Gold Bonds.** Subscribed via the funding bank's SGB application; the broker (Zerodha demat) shows the position but the actual buy never lands in any e-mailed contract note.
2. **PPF.** The PPF custodian sends no e-statement at all. Contributions show up only as debit transfers on the funding savings account.

Without doing anything, the XIRR engine sees:
- A bond / PPF with terminal `+current_value` inflow and no matching outflow → either bogus XIRR (positive-only stream) or 0% (when stored cost ≈ stored current).
- A retirement corpus that ignores the PPF entirely.

## Decision

Synthesize cashflows for these two asset classes inside the engine, on the fly, never persisted as fake transactions:

1. **SGB** — `_SGB_TRANCHES` lookup table (hand-curated from the RBI issuance calendar) maps each ISIN to `(issue_date, redemption_date, issue_price_per_gram)`. For each bond holding with no traced buy, append `(issue_date, -(units × issue_price))` to the bucket's cashflow stream and override the stored `current_value` with `units × live_sgb_price` from `metal_prices.get_metal_prices()`. Fallback for unknown ISINs: `issue_date = maturity_year − 8 years`, broker `avg_cost_nav` as a lower-confidence cost basis.

2. **PPF via transfers** — separate engine module `ppf_from_transfers.py`. Scans every owner's bank debits for descriptions matching a PPF-transfer regex (`PPF`, `Public Provident`, `SUN DEP-ONLINE PPF`, `/PPF/`, etc.). Aggregates into a synthetic `ManualAsset` row (asset_type='ppf', name="<institution> PPF (synthesised from transfers)") with one `ManualAssetContribution` per detected debit. `current_value` = compound principal at 7.1% quarterly from each contribution's date to today.

Both paths are idempotent and skip themselves if a real, statement-derived row for the same ISIN / institution already exists — the moment a true e-statement arrives, the synthetic data steps aside.

## Consequences

- ✅ Total XIRR includes SGB and PPF correctly: previously the bonds bucket was `excluded_from_total = true`; now contributes a real cost basis and a market-derived current.
- ✅ Retirement corpus picks up PPF automatically when the funding savings account is parsed — no manual entry needed for Union Bank-style cases.
- ✅ The XIRR engine still treats the synthetic ContribRows as first-class flows, so per-asset XIRR is correct.
- ❌ Pre-window balance not captured. PPF predating the earliest savings-statement we have is under-represented. Mitigation documented in `PRODUCTION_READINESS.md §7`.
- ❌ SGB tranche table is hand-curated. Extending it costs ~one line per ISIN; the fallback (`maturity_year − 8`) keeps the system honest for unknown tranches but uses the broker's possibly-stale avg_cost as the cost basis.

---

## Addendum: AGTS annual synthetics replaced by tradebook imports (2026-07)

### Problem

Zerodha's "Annual Global Trading Statement" (AGTS) is a fiscal-year aggregate: one row per (ISIN, exchange-segment) with total buy qty/value and total sell qty/value, no individual trade dates. When imported as synthetic cashflows (one debit on Apr 1, one credit on Mar 31), the aggregation error inflates XIRR for profitable years and deflates it for loss years. A FY with most trades clustered in Q4 is especially mis-dated.

### What we did

Replaced AGTS FY2022-23 (31 rows) and FY2023-24 (39 rows) with exact-dated individual trades from the Zerodha EQ-2023 tradebook CSV. Because AGTS FY2022-23 spans Apr 2022 – Mar 2023 and the tradebook is calendar-year (Jan–Dec 2023), the overlap was handled as follows:

| Period | Source |
|--------|--------|
| Apr 2022 – Dec 2022 (CY2022 gap) | 25 synthetic rows computed as `AGTS FY2022-23 total − EQ-2023 Q1 activity` per ISIN, dated to the nearest known trade date within that window |
| Jan 2023 – Dec 2023 | 226 exact EQ-2023 tradebook rows with Trade IDs and exact dates |
| Jan 2024 – Dec 2024 | Email-parsed contract notes (fully covered; no gap) |
| 2025–2026 | Email-parsed contract notes |

AGTS FY2023-24 gap = 0 across all instruments (EQ-2023 Apr-Dec activity exactly matched AGTS totals), so no Jan-Mar 2024 synthetics were needed.

### Why EQ-2025/2026 tradebook was NOT imported

EQ-2025 and EQ-2026 Zerodha tradebook rows are **order-level splits** of the same trades already in the DB via email-parsed contract notes. Totals per (date, ISIN, type) matched exactly. Importing would have doubled every 2025-2026 trade. Rule: if email parsing already covers a period with matching totals, skip the tradebook.

### Why MF tradebook was NOT imported

The Zerodha Coin MF account (`0c15a8ca`) already has 202 email-parsed transactions covering 2021-2026 at the consolidated (daily) level. MF tradebook rows are individual order slices of the same daily NAV-priced units. Importing would double-count.

### Fingerprint namespaces

Each source uses a distinct fingerprint prefix to prevent cross-source dedup collisions:

| Source | Prefix |
|--------|--------|
| EQ-2023 tradebook | `tradebook_eq2023_{trade_id}` |
| CY2022 gap synthetics | `tradebook_gap_cy2022_{isin}_{type}` |
| Approximate-date fills | `tradebook_cy2024_approx_{sym}_{date}_{qty}_{price}` |
| Email contract notes | `zerodha_{date}_{isin}_{qty}_{price}` |
| AGTS (legacy, now deleted) | `agts_{fy}_{isin}_{exchange}_{segment}` |

### Known open items

- 9 CY2024 trades (EMUDHRA, TATASTEEL, TITAGARH) have approximate dates (Jun-Jul 2024 window). Exact dates can be found in the physical contract notes. These are tagged with fingerprint prefix `tradebook_cy2024_approx_*` for easy identification.
- NYKAA: AGTS shows 111 units bought, 131 units sold in Nov 2023. The 20-unit difference is explained by a 5:1 bonus issue in Nov 2022 (corporate action, not in AGTS). Position is fully closed; cost-basis impact is ~₹3,420 of untracked free units.


## Revisited (2026-10, ADR-0026)

The principle stands — nothing is invented. What changed: (1) SGB cost now lives in the ledger (`app/ledger/sgb.py`, rule `issue_price_cost`) instead of being synthesised inside the XIRR engine; (2) PPF built from transfers uses the Government's published rate history and PPF's own interest rule (`app/engine/small_savings.py`); (3) units the depository shows without a trade are turned into **persisted** `derived` trades (`app/ledger/derive.py`) — a deliberate reversal of "never persisted": the ledger reconciles statements against trades, so the evidence must be a row. They are marked `source='derived'`, rebuilt from scratch every refresh, and replaced the moment a real trade arrives; (4) the break-even *redemption* synthesis was removed entirely, because an assumed 0% return is not evidence (`tests/test_no_invented_data.py`).
