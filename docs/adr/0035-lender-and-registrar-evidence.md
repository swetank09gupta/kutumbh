# ADR-0035: Lenders and registrars already mail the missing facts; the update window follows the last good run

**Status:** Accepted
**Date:** 2026-10

## Context
Chasing the "known gaps" (ADR-0034) showed that most of them were mail we already received but did not read:
- Newer Union Bank statements were missing because the scheduler fetched a **fixed 7 days**: any pause longer than a week left a permanent hole. Older ones were locked because their password rule was unknown, yet the bank's own mail states it (2020-21: the account number; 2024+: first four letters of the name + DDMM).
- `IT_PROV…`/`IT_FINAL…` PDFs are **HDFC home-loan interest/principal certificates** (password: loan account number): the lender's own figures per financial year, which our schedule could only estimate (and not at all before its first letter, Sep 2024).
- The sales with "no known cost" are an IPO allotment (LIC) and a bonus issue (Nykaa). The **registrar's advice** states quantity, issue price and the demat credit.
- Every SGB bought online was ₹50 a gram cheaper than the nominal price.

## Decision
- **Update window:** `pipeline.catch_up_window` = since the last successful run + 3 days overlap, clamped to 7–400 days. Scheduler, Sync button and reprocess share it.
- **Sender domains:** Union Bank's 2026 statements come from `ubi.bank.in`; the IMAP search is by domain and Gmail matches whole words only, so every new sender domain must be listed in `KNOWN_SENDERS` (a bare `unionbank` key does not find `noreplyunionbank@…`).
- **Passwords** come from private config only (`union_bank_account_no`, `hdfc_loan_account_no`) and are tried by `_all_pdf_passwords`; nothing is stored in the repo.
- **Loan certificates** (`loan_tax_certificates`, key `HDFC|owner|fy|kind|doc_date`) outrank everything for a past year: final > schedule (current FY) > provisional. The loan card and tax view show the lender's principal/interest per FY; prepayments count for 80C as HDFC's own note says.
- **Registrar notices** become trades only when matched to a demat credit of the same quantity: IPO allotment → `ipo_allotment` at the issue price; bonus → `bonus` at nil cost (a known zero, not a missing price). They are never merged with same-day trades.
- **SGB cost** defaults to the online price (nominal − ₹50); a bank subscription debit still overrides; coupons stay on nominal.
- **Dividends:** the match window is 120 days (final dividends follow the AGM); the per-share amount is also tried as paid before later splits/bonuses; whole-rupee payouts are accepted; and where the credit names the stock, the sum is a whole number of its shares and our trades say nothing about holdings (bought before the history), it is matched on name and arithmetic and flagged as such. Where our trades do state the holding, the arithmetic must fit them.

## Found while loading it
Two parsers invented rows from unrelated PDFs and were fixed (parse cache `v16`): the SBI parser took the fund house "PPFAS" in a Zerodha statement for a PPF account and asked the LLM for a balance (₹210.66); the Zerodha parser read the "Sub Total" row of a daily margin statement as a ₹4,385 holding.

## Not recoverable from mail
The wife's pre-2020 sale costs (no note exists), her unrecorded ₹15,000 PPF funding account, balances of accounts that no longer send mail.

## Consequences
Tests: `test_pdf_passwords`, `test_loan_certificate`, `test_loan_report_db`, `test_registrar_notices`, `test_automation` (window), `test_sgb*`, `test_dividends_tax_db`.
