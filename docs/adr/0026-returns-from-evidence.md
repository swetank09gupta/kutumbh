# ADR-0026: Returns come from one position model, built from evidence — never from guesses

**Status:** Accepted
**Date:** 2026-10

## Context

The Investments page showed "—" for equity ("opened and closed within days"), for mutual funds ("history incomplete for 50%") and for every goal, and a headline "Total 16.3%" that silently left out the ₹29L of funds. Four things were wrong, none of them the data itself:

1. **Three code paths** (class, goal, instrument) each assembled cash flows with their own fall-backs, so their numbers could not add up.
2. **Newton's method** returned nothing for flows that change sign several times, and the fallback message blamed "positions opened and closed within days".
3. **All-or-nothing withholding:** one fund without history hid the whole class and every goal containing it.
4. **Missing history that was recoverable.** A Coin mail that renamed a fund resolved to the wrong ISIN; a trade settling over month-end left a 1.9-unit "unknown" lot; SGBs have a published issue price; the wife's SIP units were in the depository lines with public NAVs and her bank debits; Coin cash-flow rows carried no ISIN; `synth_ghost_redemptions` invented break-even credits for funds with no sale on file.

## Decision

**One model** (`app/engine/portfolio_xirr.py`): `build_positions()` yields every held, closed and manual investment with its own dated flows; total, class, goal and instrument are sums over the same positions (class rows add up to the total by construction).

**Cost coverage, not withholding:** a position joins a return only if its cost is known (ledger quality `exact`; a closed position needs units bought = units sold). The rest is excluded *and named* ("on 96% of the value — ₹1.26L has no purchase history yet"). Nothing is estimated into a return.

**Honest rates:** XIRR is solved by bracketing (`xirr_engine.py`), and money held under a year on average is shown as plain gain (the SEBI/AMFI convention) instead of an annualised number.

**Recover history from evidence** (`app/ledger/`):
- `boundary_indices` — a trade settling across a month-end (+q then −q) no longer leaves an unknown lot.
- `isin_override` — mirror residuals (+q on one ISIN, −q on another, same interval) re-point that one trade.
- SGB units cost the RBI issue price from the issue date (`sgb.py`).
- `derive.py` — a demat credit no trade explains becomes a `derived` trade: units and date from the line, price from the AMFI NAV that day (mfapi.in history), cash from the matching bank debit when there is one. Re-derived from scratch on every refresh, so a real Coin/tradebook trade replaces it; never derived unless the lines add up to the statement's own change.
- Cash-flow rows take the ISIN of their ledger trade; the break-even synthesis is gone.

**Home loan:** the lender's part-prepayment letters (`loan_anchors`, `app/engine/loan.py`) give the principal outstanding; EMIs since then reduce it by `EMI − principal×rate/12` (reproduced the lender's next figure to the rupee). It updates the liability row only when newer than what the row says, and net-worth history uses the balance of each date.

## Consequences

- Family coverage went from 50% to 98%; the remaining gap is a few units of one fund bought before any evidence we hold. A Console MF tradebook CSV would close it and replaces derived trades automatically.
- Derived trades depend on mfapi.in being reachable; without NAV a unit credit simply stays "unknown cost".
- FD and PPF rates are accrued at their stated rate, so their "XIRR" restates that rate.
