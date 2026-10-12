# ADR-0034: Old contract notes are read, and two identical lines of one mail are two orders

**Status:** Accepted
**Date:** 2026-10

## Context
The tax view listed sales whose purchase was "not on file". Chasing them in the mailboxes and raw files found three separate causes:
1. **Two orders collapsed into one.** The Coin mail of 8 Dec 2021 prints the Axis Nifty 100 SIP instalment twice (two SIP orders, same folio, date and amount). The trade identity had no position within the mail, so the pair became one trade and 99.938 units were missing: the exact size of the "unknown" sale.
2. **An old layout was not read.** The 2018 Zerodha notes print trade rows without an exchange token and without an ISIN, so the row pattern never matched.
3. **A note on disk never stored.** The 16 Mar 2020 note parsed to 3 trades but was not in the ledger.

## Decision
- A Coin line's identity gains an **ordinal among identical lines of the same mail** (the first keeps its old key, so nothing existing changes; later ones end `|#n`). A second identical line is a second order. The duplicate guard added in ADR-0029 now compares owner, folio (when printed), date, side, units, amount **and ordinal**, and also covers redemptions, which print no folio; the maintenance cleanup does the same.
- The contract-note row pattern accepts the 2018 layout; a note with no ISIN takes the ISIN the **same ticker** already has in the ledger when that is unambiguous, otherwise stays unresolved.
- Older notes were re-read from the raw files and the 2017–2020 mailbox window was fetched (idempotent): 5 trades recovered for the wife (BEL 2018, ONGC and TCS Mar 2020), 1 for the user.

## What remains unknown, and why
The user's LIC and Nykaa sales (units from an IPO allotment and a bonus issue: no contract note exists; the price is public but which allotment category applied is not) and the wife's BEL, HDFC, SBIN, Reliance and Vedanta units sold in 2020 (bought before the earliest contract note in either mailbox). They are listed by the tax view, not costed.

## Consequences
Parse cache `v13`. Tests: `tests/test_contract_note_2018.py`, `tests/test_coin_duplicates_db.py` (identical-line cases, a refless duplicate, mutation-checked).
