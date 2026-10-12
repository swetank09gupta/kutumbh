# ADR-0033: Axis statements are read by column and accepted only if they reconcile

**Status:** Accepted
**Date:** 2026-10

## Context
The wife's Axis account held 233 rows, 180 of them inconsistent with their own balance column, and 17 rows of exactly ₹100,000 or ₹685,000. Reading the raw statements showed why: the PDF text parser took every number on a dated line, and the same pages carry a "relationship summary" (`Fixed Deposit INR 100,000.00`) and a "summary of deposits" whose figures look like amounts. Newer statements also print no running balance per row, so debit versus credit cannot be told from the text. The five ₹100,000 "interest income" debits that looked like transfers never happened; the cashflow had counted them as ₹5 lakh of expenses.

## Decision
`app/ingestion/parsers/axis_layout.py` reads the transaction table by **column position** (the x position of the Withdrawals / Deposits / Balance headers decides what a number is) and accepts a block only if `opening + deposits - withdrawals = closing`, the balances the statement itself prints. A statement that does not reconcile stores nothing (a warning, no rows). Term-deposit ledgers (header with "Value Date") are separate blocks. Running term deposits come from "Summary of Deposits" (number, amount, maturity value, rate, dates) and become `Axis FD ...nnnn` assets with deposit snapshots. Parse cache `v12`.
"Is this deposit still live" is judged per **bank** (the newest table of the bank it is with), because Axis and SBI statements are different tables; an Axis deposit is no longer measured against SBI's newer statement.
The old rows were replaced from the raw statements (database backed up first): 40 statements, 250 rows, all reconciled.

## Consequences
Found by the repair: a ₹100,000 Axis term deposit (6.6%, opened 4 Oct 2025, ₹110,308 at maturity) funded from the owner's SBI the same day, which was in no asset list. Net worth +₹1.06 L. The wife's FY2020-21 expenses fell by about ₹4 lakh of fake debits. Tests: `tests/test_axis_layout.py` (synthetic layouts with the real column geometry and the traps; mutation-checked).

## Addendum: where a deposit's growth starts
Axis prints the *opening* principal and no accrued column, while SBI's printed principal plus accrued interest is true on the statement date. Deposit growth was anchored at the statement date for both, which valued the Axis FD ~₹640 low and implied 8.12% instead of about 6.8%. The asset's notes now carry "Principal as of <open date>" (`ParsedManualAsset.principal_as_of`) and `deposits._anchor_date` starts growth there when no accrued interest is printed. Parse cache `v14`.
