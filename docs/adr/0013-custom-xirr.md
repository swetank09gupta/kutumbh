# ADR-0013: Custom XIRR (Newton–Raphson) over numpy-financial

**Status:** Accepted
**Date:** 2024-09 · **Updated:** 2026-07

## Context

Portfolio XIRR is needed at four roll-up levels: per-instrument, per-asset-class, per-owner, and family. Cashflow streams range from ~50 to ~2,800 entries.

## Options

- **`numpy_financial.xirr`** — deprecated upstream; pulls numpy into the runtime closure.
- **`scipy.optimize.brentq` over NPV** — works but fragile near tax-loss-harvesting flows where NPV crosses zero multiple times. Heavy dep.
- **Custom Newton–Raphson with bisection fallback** — ~50 lines, no extra deps, controllable convergence.

## Decision

Custom implementation in `app/engine/portfolio_xirr.py`:
- Newton–Raphson, initial guess 0.1, tolerance 1e-6, max 100 iterations.
- Falls back to bisection on `[-0.999, 10.0]` when Newton diverges or the derivative goes near-zero.

### Transaction scope (updated 2026-07)

`portfolio_returns` in `app/api/routes/accounts.py` now fetches transactions from **all accounts** for the owner set, not just investment-type accounts. Rationale: ISIN-tagged SIP debits arrive via bank accounts (e.g. a Union Bank mandate debit tagged with an MF ISIN), and restricting to broker accounts missed them entirely. The wider pull is safe because `Transaction.isin IS NOT NULL` is the gate for per-instrument XIRR inclusion.

### Holdings scope (updated 2026-07)

Holdings are filtered by `holding_type ∈ {stocks, equity, mutual_fund, bonds}` rather than by `account_type`. This picks up CDSL/CAMS/KFintech holdings that carry a different `account_type` but the same holding semantics.

### Bucket attribution for historical transactions (updated 2026-07)

Historical transactions (closed positions, pre-email instruments) may have an ISIN that no current holding carries. Bucket is resolved in priority order:

1. `isin_to_type` map built from current holdings (covers the common case).
2. ISIN prefix fallback: `INE…` → equity, `INF…` → mutual_fund, `IN0…` → bonds.
3. Majority bucket of the account if ISIN is absent.

Without the prefix fallback, closed equity positions were falling into whatever the account's majority bucket was (often mutual_fund), inflating MF XIRR and deflating equity XIRR.

### Dedup view ORDER BY (updated 2026-07)

`latest_holdings_deduped` previously ordered `(institution_priority, snapshot_date DESC)`. Fixed to `(snapshot_date DESC, institution_priority)` — most-recent snapshot wins first, then authority tier breaks ties within the same date. The prior order could surface a stale high-authority snapshot over a fresher lower-authority one.

## Consequences

- ✅ Matches Excel `XIRR()` to ±1e-4 across 12 fixture cashflows.
- ✅ 38ms for a 2,800-flow family aggregate; fast enough to compute on every API call.
- ✅ Zero extra dependency.
- ✅ ISIN-tagged bank-mandate SIPs now contribute to correct per-instrument and per-type XIRR.
- ✅ Closed equity positions no longer inflate MF XIRR.
- ❌ The fallback logic adds ~20 lines of code that needed careful testing on adversarial cashflows.
- ❌ Fetching all accounts widens the transaction scan; negligible on household scale (~10k rows) but worth noting if the DB grows.
