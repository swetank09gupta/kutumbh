# ADR-0041: Console tradebooks fill what notes and mails miss; a trade's cashflow link must survive re-keying

**Status:** Accepted
**Date:** 2026-10

## Context
The wife's Console tradebooks (equity, 2018, 2020, 2021; no trades in 2017 or 2019) were supplied as `.xlsx`. Against the ledger they added 49 trades (e.g. 36 HDFC and 200 NTPC buys, 77 ONGC, 70 SBIN, 12 Reliance, 192 Vedanta), closed the Reliance "earlier buy missing" error and matched the Vedanta dividend. Importing them exposed four defects:
1. **Contract-note dates.** 27 of the wife's 2020 notes were dated two days late: the header prints `TRADE DATE 2020-06-23 23/06/2020 … SETTLEMENT DATE 25/06/2020`, the parser's date pattern does not read the year-first form and fell through to the settlement date. All 543 of the user's trades were right.
2. **Shared cashflow.** With the dates fixed, a note and its tradebook row (same Trade ID) share one cashflow row (Zerodha trades are fingerprinted by owner, ISIN, date, amount, direction). Removing the lower-ranked duplicate trade deleted that shared row and left the kept trade without a cashflow (37 trades; XIRR would miss them).
3. **Broker re-keying.** ADR-0038's row identity (direction and amount) was applied to broker rows too; trades point at their cashflow row by fingerprint, so 216 trades lost their link and drifted XIRR, found only because the issue list jumped from 2 to 216.
4. **Links that go stale** for the same reason whenever a row's fingerprint changes (an ISIN filled in later): re-reading the file never repaired the trade's link.

## Decision
- **Read tradebooks** (`.xlsx` via a standard-library reader, `.csv` as before); the ledger already ranks contract notes above tradebooks above derived trades.
- **Contract-note trade date**: a year-first `TRADE DATE` is read first; the settlement date is never taken for it. `ZerodhaParser._PARSER_VERSION` bumped (only Zerodha files re-read).
- **Revises ADR-0038:** the direction-and-amount identity is for **bank** accounts only; broker rows keep their previous key.
- **Cross-source duplicate removal keeps a cashflow row that a kept trade points at.**
- **Links follow fingerprints:** maintenance moves trades to a row's new fingerprint when it re-keys it, heals a dead link when exactly one row of the account has the same date, amount and reference (`_relink_trades`), and a re-read points an existing trade at the current fingerprint.

## Consequences
Tests: `tests/test_ledger_shared_cashflow_db.py`, `test_zerodha_contract_notes.py` (`ContractNoteTradeDate`), `test_bank_statement_gaps.py` (broker key). The 31 mis-dated notes' rows were replaced (old rows deleted by `source_file`, files re-read); two 2018 BEL notes had been stored under an absolute path and needed their duplicate cashflows removed by hand. Wife's XIRR 13.04% → 14.66% (more history); no other figure moved.
