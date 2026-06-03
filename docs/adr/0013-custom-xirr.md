# ADR-0013: Custom XIRR (Newton–Raphson) over numpy-financial

**Status:** Accepted
**Date:** 2024-09

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

## Consequences

- ✅ Matches Excel `XIRR()` to ±1e-4 across 12 fixture cashflows.
- ✅ 38ms for a 2,800-flow family aggregate; fast enough to compute on every API call.
- ✅ Zero extra dependency.
- ❌ The fallback logic adds ~20 lines of code that needed careful testing on adversarial cashflows.
