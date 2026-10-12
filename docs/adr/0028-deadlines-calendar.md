# ADR-0028: A deadlines calendar where every date cites a rule or its evidence

**Status:** Accepted
**Date:** 2026-10

## Context
Dates that matter (the PPF 5th-of-month and FY headroom, deposit maturities, ELSS unlocks, SGB maturity, a repo change reaching the loan) were nowhere in the app. They come from two sources only: public rules (now in the registry, ADR-0027) and the family's own statements.

## Decision
`app/engine/deadlines.py` builds one list (`GET /api/deadlines/`, "Coming up" card on Overview, filtered by Family/Self/Spouse). Each item carries `rules` (registry ids, resolved on the date) and/or `evidence` (the statement, letter or trade it was read from). `uncited()` returns items with neither; the consistency check `deadlines_cite_a_basis` (severity *fail*) and a mutation test hold it empty.
- **No guessing:** a PPF whose opening date is unknown is listed under `gaps`, not given a maturity. PPF headroom counts recorded deposits only and says so.
- **Deposits** use the existing "closed = absent from the newest statement" rule; a deposit that matured within 30 days and is still listed is flagged.
- **ELSS** unlocks are three years after each instalment, sales consume oldest units first, grouped by month.
- **SGB:** maturity only. Early-exit dates and comparison are not built (decision 12 Oct 2026; RBI also sets each tranche's request window, so it is not a fixed rule).
- **Loan:** a repo change within 90 days shows against the lender's last letter; the reset date is not in the letters, so none is given (forecast: Phase 2).
- TDS threshold tracking moved to Phase 4 (needs interest per payer per year, not just notices).

## Consequences
New registry rules: `ppf.interest_cutoff_day`, `ppf.maturity_years`, `elss.lock_in_years`, `sgb.tenor_years`. `tests/test_deadlines.py` (date rules, scratch household with a fixed today, the guard's mutation). No figure on any page moved.
