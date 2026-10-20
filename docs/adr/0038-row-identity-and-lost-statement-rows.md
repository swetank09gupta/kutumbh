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

## Follow-up (same week)
- **Cross-file duplicate removal** now keeps two rows whose printed running balances differ, whatever their narrations share: two ₹80 debits to one shop share its QR code, two ₹3.26 NEFT credits share the date inside their references; both pairs had been merged into one (the balance is the proof, as in ADR-0037).
- **Axis letters** (`RD_LETTER`, `DORMANT_LETTER`) are not statements. Read as one they created empty "accounts" named after the wife's two recurring-deposit numbers (…6646, …8187; both matured Mar 2025) — the origin of the wrong "Axis 6646" label.
- **Two masks of one account number** (`50410XXXXX` and `XXXXXX3237`, the ex-Citi account; the dormant letter confirms 3237) are merged by maintenance when they have the same width and agree wherever both show a digit.
- **A chain break after a month with no rows and no statement balance names that month** ("no statement for 2024-10"): it is that month's net movement, not a lost row. Three exist (SBI 8660 Oct 2024, SBI 9132 Aug 2025, Axis 3856 May 2025); the banks never mailed those statements, so they stay as named gaps.
- **"Bank balances are recent"** compares each account with its own rhythm (median gap between its balances, at least a month) plus a 25-day delivery lag, and ignores ₹0 balances; the fixed 45-day limit flagged a monthly statement that was merely due.

## Consequences
Parse cache `v18`; the Axis parser carries its own suffix (`_CACHE_SCHEMA` is a class attribute), so a change to one parser re-reads only its files. Tests: `tests/test_bank_statement_gaps.py`, `test_identical_bank_rows_db` (reversal kept; ref-only row re-keyed). Known and not fixed: a few 2021 Axis statements bundle several accounts (…3856, …5859, …6463) and are read as one.
