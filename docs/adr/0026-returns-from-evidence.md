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

**PPF and deposits** (`app/engine/small_savings.py`, `portfolio_xirr.py`): the PPF rate is read from the Government's own table (National Savings Institute, full history since 1968; cached a week) and applied by PPF's own rule — interest on the lowest balance between the 5th and month-end, credited each 31 March. The bank's intermediate certificate is bundled (`config/certs/nsi-chain.pem`) because that site does not send it. A fixed deposit's return is *measured* from the interest it actually paid into savings (the deposit's account number is in the "INT TRF FRM … TO …" narration) when nearly every monthly credit is on file; otherwise it is shown as a balance at the rate printed on the deposit. The SBI statement's own columns give a deposit's worth: **principal + interest accrued** at the statement date (the parser reads the *Interest Accrued* column, previously discarded), grown along the contract to the **maturity value** for cumulative deposits (`app/engine/deposits.py`); net worth, Investments and Manual Assets all call the same function (INV-03, INV-09).

**Statement history** (`deposit_snapshots`, `app/engine/deposit_flows.py`): every monthly SBI statement prints each deposit's principal, interest accrued and maturity value, and the PPF balance. Each reading is kept (new statements record themselves; `scripts/backfill_deposit_history.py` read the 96 on disk; LLM-read rows are never kept). From the series: a **PPF's contributions are its balance steps** (minus the 31-March interest by PPF's own rule; bank transfers date them exactly where the bank statement exists), measured from the first financial year-end; a **deposit's part-withdrawals and top-ups** are drops and non-interest rises in its principal; and a deposit the **newest statement no longer lists is closed** and is worth nothing (two such rows had been counted — ₹1.84L — and are now excluded everywhere).

**Interest reconciliation.** A deposit that pays its interest out monthly shows those payments as credits in savings ("INT TRF FRM <deposit> TO <savings>"). They are one fact seen from two sides: *income* in the cash-flow view, an *inflow of the deposit* in the returns view; net worth carries only principal plus the small unpaid accrual, so nothing is counted twice. The credits are **net of tax deducted at source** (about 10% from July 2026: ₹518 became ₹466 on ₹1,00,000), so SBI's "TDS of INR … deducted for interest paid on A/C …" notices are read (`tds_deductions`) and added back: the return is quoted before tax, like the PPF and the funds.

**PPF value and return.** A PPF's interest is credited **once, on 31 March, and not before**, so a PPF is worth exactly its statement balance on every page (nothing accrued is added; a wrongly added accrual was removed). Its return is measured only between 31-March balances (`ppf_measure`): for each whole year, *opening + deposits + interest by PPF's own rule (5th-to-month-end, notified rate) = closing* is reconciled to the rupee, deposits coming from the bank transfers. A rise they do not explain is looked for among round, unlabelled own-account transfers ("INB MBS") and accepted only if it closes the year; what remains is a deposit the bank statements do not show, its month inferred from the interest it earned and flagged (`/api/health/ppf`, consistency check `ppf_years_reconcile`). Measured this way both PPFs return 7.1%, the notified rate; the FY 2024-25 reconciliation of one PPF matches to the rupee.

## Consequences

- Family coverage went from 50% to 98%; the remaining gap is a few units of one fund bought before any evidence we hold. A Console MF tradebook CSV would close it and replaces derived trades automatically.
- Derived trades depend on mfapi.in being reachable; without NAV a unit credit simply stays "unknown cost".
- FD and PPF rates are accrued at their stated rate, so their "XIRR" restates that rate.
