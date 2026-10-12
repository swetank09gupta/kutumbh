# ADR-0025: Bank alert e-mails are evidence, not cashflow

**Status:** Accepted
**Date:** 2026-10

## Context

Statements arrive 3–6 weeks late (HDFC stopped at 21 Sep, SBI at 31 Aug, when it was 11 Oct), but both banks e-mail an alert within a minute of every transaction, to the user's Gmail and the spouse's Yahoo. Alerts cannot enrich HDFC payees — the HDFC statement narration already holds the full name, VPA and UTR — and SBI alerts name only the counterparty of transfers. Their value is **time and cross-checking**.

## Decision

Alerts are parsed (`app/ingestion/parsers/bank_alert.py`) from IMAP into `bank_alerts` (natural key, idempotent) on every sync (`alert_fetcher.py`). **Revisited (ADR-0037):** pending UPI payments to a named payee now enter `v_cashflow` as provisional spending (a floor, flagged, gone once the statement matches). Everything else below still holds. Original text: they are **never added to `v_cashflow`**: a transaction would otherwise exist twice once the statement lands, and the single-definition rule of ADR-0024 would break. `app/txn/alert_recon.py` instead gives each alert one status:

| status | meaning |
|---|---|
| matched | a statement row confirms it (UTR first, else date ±2 days + amount + direction; one-to-one) |
| pending | newer than the newest statement row for that bank — shown on Transactions as "Since the last statement", outside every total |
| unmatched | inside a statement's date range but no such row → the statement parse lost a transaction (consistency **warn**) |
| untracked | account that sends alerts but whose statements we never get (SBI RD/FD/sweep accounts) — listed with its last balance |
| dup | the same event announced twice (HDFC ACH sends two mails; SBI sends CBSSBI plus NEFT/SI mail) |
| outside | older than the first statement we hold |

SBI alerts carry the real account number, statements only a masked reference, so an account counts as tracked once one of its alerts matches a row.

## Consequences

- `/api/health/alerts` and the `alerts_have_statement_rows` check expose parser gaps the day statements arrive.
- Today's bank balance in net worth is the newest of the statement balance and the alert chain (`alert_balance`): last alert that prints a balance, plus later alerts that print none (HDFC UPI). Only for accounts that already have statements; history still uses statements.
- SBI deposit/sweep accounts have no statements; their alerts are compared with the Manual Assets rows (`deposit_accounts_match_alerts`).
- A new alert wording that does not parse is silently skipped; `PARSERS.md` lists the supported templates.
