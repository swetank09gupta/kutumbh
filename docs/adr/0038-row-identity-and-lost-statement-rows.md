# ADR-0038: A bank row's identity includes direction and amount; first-row amounts are read, not skipped; Axis moved domain

**Status:** Accepted
**Date:** 2026-10

## Context
The balance-chain check (ADR-0037) named days where the printed balance jumped by a round amount with no row to explain it. Tracing them in HDFC 1632 found real rows we had lost, in two different places:
1. **Identity.** A row with a reference was identified by `account|date|reference`. A UPI reversal credit carries the date and reference of the debit it undoes, so the credit was dropped as "already seen" (₹50,000 on 7 Apr 2025, ₹15,000 on 11 Mar 2026; no `REV-UPI` row existed anywhere).
2. **First row of a statement.** HDFC's stacked PDF table gives amounts by balance difference, which needs the previous balance. A statement that begins on a day with no "Opening Balance" line had none, so its first row was skipped: ₹1,80,000 (2 Jan 2025) and ₹2,00,000 (1 Jan 2026) own-account NEFT credits.

Separately, Axis statements arrive from `statements@axis.bank.in` since 2026 (we only knew `axisbank.com`), so the wife's account stopped at Dec 2025; and Axis PDFs carried no account number, so every statement landed on "unknown" and was folded into whichever account sorted first (`Axis 6646`, which is not her account; the statements say `…3856`).

## Decision
- **Identity** is `account|date|reference|direction|amount` (amount normalised, so `50000` = `50000.00`; reference normalised, so padding does not matter). The same payment in two overlapping statements still has one identity; a reversal, or a second amount on the same reference, has its own. Existing rows are re-keyed by the normal maintenance pass (it already recomputes every stored fingerprint and merges true duplicates). This revises the reference-only identity behind decision 12 and the HDFC UTR-padding fix in `dedup.py`: the padding fix stays, the missing direction and amount are added.
- **First row**: when no opening balance is printed, its type and amount are taken from the Withdrawal/Deposit column, only if that column holds exactly one more amount than the later rows' deltas explain and the other column matches exactly. Otherwise the row is still skipped, never guessed.
- **Axis**: accept `statements@axis.bank.in` (exact address; `Straight2axis@axis.bank.in` sends payment advices). Read the account number from the statement: last four digits, or the whole masked number when the bank hides the tail (the ex-Citi LIBERTY account prints `50410XXXXX`).

## Consequences
Parse cache `v18`. Tests: `tests/test_bank_statement_gaps.py`, `test_identical_bank_rows_db` (reversal kept; ref-only row re-keyed). Known and not fixed: a few 2021 Axis statements bundle several accounts (…3856, …5859, …6463) and are read as one.
