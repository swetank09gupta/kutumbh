# ADR-0030: The home loan is reconciled against every lender letter, and the forecast follows the repo

**Status:** Accepted
**Date:** 2026-10

## Context
The loan balance came from the newest HDFC prepayment letter rolled forward by EMIs (ADR-0026), but nothing checked the roll against the other twelve letters, nothing said when the loan ends or what prepayments achieved, and the rate after the 7 Oct 2026 repo hike was unknown. The 80C advice estimated loan principal with its own formula.

## Evidence found in the 13 letters (2024-26)
- One EMI reproduces the next letter's figure to under ₹1 (monthly-rest). 10 of 12 intervals reproduce to the rupee.
- **The lender applies its letter EMI; the debited amount can lag a revision by one instalment** (Jul and Sep 2026: debited ₹170,000 / ₹164,056, letters ₹164,056 / ₹170,000). Using the letter EMI reproduces both to ₹0.44 and ₹0.30; the debit does not.
- Rate resets land mid-interval: the 5 Dec 2025 cut reached the loan with the 5 Jan 2026 instalment (31 days), exact to the rupee.
- **Every one of the 13 letters equals the repo in force on its date + 1.85%**, so the loan passes the repo through 1:1.
- Two older intervals (Dec 2024 → Jun 2025, Jun → Nov 2025) do not reproduce: they hold two repo cuts, a missing Sep 2025 debit and the EMI date moving from the 25th to the 5th. They are reported with their size, not explained away.

## Decision
`app/engine/loan_schedule.py` (pure amortisation), `app/engine/loan_report.py` (reconciliation, payoff, interest saved, forecast, tax), `GET /api/loan/`, a Home loan card on Overview.
- The roll-forward applies the letter's EMI in every month a debit was seen (the debit proves the month was paid). This revises ADR-0026's "rolled forward by the EMIs debited".
- **Reconciliation per interval:** exact (≤ ₹2), exact if an unseen EMI was paid, exact with a rate change from instalment k (the reset month is inferred only when it fits to the rupee), or differs by ₹X with the reasons.
- **Forecast** (labelled, replaced by the next letter): expected rate = repo now + the spread, only while every letter has fitted that spread; shows the extra interest if the EMI stays and the EMI that keeps the end month. The repo history is in the rules registry.
- Interest saved is valued per prepayment on its own; the figures overlap, so no total is shown. Tenure on the first letter vs now is shown instead.
- **Tax:** s.24(b) interest capped per owner on their recorded share, s.80C principal, old regime only (the wife is on the new regime: nothing). EMI principal; part-prepayments shown apart. The ELSS dead-weight advice reads this schedule (EMI principal + the year's prepayments) instead of its own estimate.
- Check `loan_reconciles_with_lender` (warning): newest letter not reproduced, EMIs left disagree with the schedule, or an EMI due more than 4 days ago has no debit. Old unreproducible intervals do not warn forever.

## Consequences
New registry rules: repo history (6 entries), `income_tax.24b_cap`. Tests: `tests/test_loan_schedule.py`, `tests/test_loan_report_db.py` (mutation-checked: letter vs debited EMI, the inferred reset month). No figure moved.
