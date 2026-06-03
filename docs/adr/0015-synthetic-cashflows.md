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
