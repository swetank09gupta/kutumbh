# ADR-0037: Every account reconciles against its own printed balances; prices must be live or say they are not; alerts count as provisional spending

**Status:** Accepted
**Date:** 2026-10

## Context
Loading old history and a new AMFI file exposed three silent errors, each found by the owner noticing a number, not by the system:
1. **Twin rows merged.** Bank rows without a reference were identified by date + amount + type + narration, so two identical rows of one statement (two ₹1,016 SBI sweep credits on 24 Nov 2024) became one. Balances were ₹ off for years.
2. **Unexplained units.** The wife's Quant ELSS showed 22.752 units more than any trade. They were bought by a ₹10,000 debit on 26 Jun 2024, but once a 2021 statement became the first statement, the search for the paying debit only looked at the last 45 days of a 3.7-year gap.
3. **Dead prices.** AMFI split `Scheme Name` into `Scheme Name;Plan;Option` and moved the NAV to column 7. Our reader took "Direct Plan" for the NAV, every fund came out at 0 and silently fell back to its last trade or statement rate. A single failed fetch was also remembered as "no NAVs" for the life of the process, and the in-memory map never expired.

## Decision
- **Row identity.** For reference-less rows with a printed balance, an identical row is a separate transaction exactly when its running balance differs (ordinal in the fingerprint, first row keeps its old key); the same balance is one row listed twice; no balance means no assumption. Maintenance never merges an ordinal row away.
- **Continuity check, any bank** (`app/txn/continuity.py`, check `bank_balances_chain`, `/api/health/continuity`): yesterday's closing + today's credits − debits must equal a printed balance of today. A break gives the date and the amount missing: a lost or merged row, or a statement month never received (SBI wife Aug 2025, SBI user Oct 2024 were found this way and are not in any mailbox). It points; it never invents.
- **Unexplained units** are searched in the whole interval (up to 900 days), and a debit already attributed to another security is not free.
- **Prices.** AMFI reader handles both layouts; a failed fetch is retried after 10 minutes; the NAV map and file expire after 6 hours; check `prices_are_live` warns when a fund is not at an AMFI NAV or a share not at its market price (SGBs are valued from the statement rate and not judged).
- **Provisional spending** (revises ADR-0025's "never counted"): a pending alert counts as spending only if it is a UPI payment out to a named payee that is not the household and not a transfer/investment/EMI by the categoriser (`app/txn/provisional.py`). Transfers (the alert names the payee, often truncated), sweeps, SIP/ACH debits and all credits wait for the statement. It enters `v_cashflow` with `provisional = true`, is shown as such, and leaves in the same rebuild in which the statement matches it. So it is a floor of real spending and never double counted.
- A dormant Axis account (opening = closing, no rows) is reconciled, not a warning.

## Revisited
ADR-0038: rows *with* a reference now also carry direction and amount in their identity (a reversal credit was dropped).

## Consequences
Parse cache `v17`. Tests: `test_identical_bank_rows_db`, `test_continuity`, `test_derive_db` (long interval, attributed debit), `test_amfi_navall`, `test_prices_live_db`, `test_provisional_db`, `test_axis_layout`. Figures moved: funds are valued at live NAVs (≈ −₹12,000 family-wide against the stale prices).
