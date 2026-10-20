# ADR-0040: The bank's own alert mails explain a month whose statement never came

**Status:** Accepted
**Date:** 2026-10

## Context
Three bank accounts have a month with no statement (SBI 8660 Oct 2024, SBI 9132 Aug 2025, Axis 3856 May 2025): the banks never mailed it, so the balance chain (ADR-0037) breaks by that month's net movement. The mailboxes do hold what the bank did send: an alert mail per transaction. For the Axis month they add up to the break to the paisa (₹1,318 + ₹5,000 + ₹1,000 out, ₹661 in = ₹6,657), but we read no Axis alerts, and no SBI alerts from before `alerts.sbi.bank.in` (the older domain is `alerts.sbi.co.in`, the same kind of domain move as Axis and Union Bank).

## Decision
- **Read more alerts.** Axis (`alerts@axisbank.com`, `alerts@axis.bank.in`; two wordings, UPI/NEFT info, no balance), SBI from `alerts.sbi.co.in`, and two SBI templates that were unparsed: debit-card ATM cash withdrawal and YONO NEFT (no date in the body: the day it was sent; no ref, so a "NEFT Transaction" mail for the same transfer counts as the original and this one as its copy). Card purchases name the card, not the account, and are skipped. Axis logins and `Straight2axis@` payment advices are not transactions.
- **Alerts stay evidence, never cashflow** (ADR-0025): no statement row is created from them.
- **A chain break across a missing month is explained when that month's alerts (copies excluded) add up to it exactly** (`continuity.alert_evidence`; account ↔ alert accounts by `alert_recon.account_alerts`, the mapping `alert_balance` already used). An explained month is not a warning and is listed under `explained` in the check; a partial or contrary sum stays a warning that shows both ("7 alert(s) show a net credit of ₹A, the break is a net debit of ₹B").

- **Alert status `gap`**: an alert inside the statements' range whose month has no statement at all (no row, no balance for that owner and bank) is not a lost statement row, so it no longer counts as `unmatched`. It stays evidence.
- **Old alerts do not outrank newer facts**: a deposit alert older than the Manual Asset's statement is ignored in the alert-versus-asset comparison, and a deposit account silent for 135 days (interest mails at least quarterly) is treated as closed, not as "money we do not record". Found when the alert history from Oct 2024 was first loaded.

## Consequences
Tests: `tests/test_bank_alert.py` (real wordings, names removed), `test_identical_bank_rows_db` (explained / partial / copies not counted). Newer Axis and SBI alerts after the last statement now show as pending and, for named UPI payees, as provisional spending under ADR-0037's rules.
