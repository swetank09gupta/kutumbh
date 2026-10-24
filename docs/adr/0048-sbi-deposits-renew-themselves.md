# ADR-0048: SBI deposits that renew themselves

**Status:** Accepted
**Date:** 2026-10-24

## Context
SBI FD ...6925 (₹1.4 L, opened 11 Oct 2024) reached its maturity date on 11 Oct 2026. The app took it as matured: it left the FD total, and (the savings balance being SBI's printed balance less the live deposits) its ₹1.4 L showed up as cash. The owner reported it had renewed itself (online it now shows 11 Oct 2027).
The bank sends no mail for a renewal, and the newest statement (30 Sep) predates it. The same will happen to every SBI deposit left on the default instruction.

## Decision
`alert_recon.renew_matured_deposits` (run with each alert reconciliation): a listed SBI deposit past its maturity (and not covered by a statement dated after it) whose owner's SBI alerts show no credit of at least half its balance since that date was not paid out: it stays live, its maturity moves one year on
(`RENEW_TERM_YEARS`; the real term is unknown until the next statement), and its notes say so. The next statement's FD table replaces maturity, term and rate (existing newer-statement rule). A payout credit alert keeps it matured. Nothing is entered by hand.

## Consequences
FD ...6925 is a live deposit again (maturity 2027-10-11, which happens to equal the real one). SBI savings for the owner falls from ₹1.52 L to ₹0.12 L and deposits rise by the same ₹1.4 L; the emergency fund total is unchanged (₹21.13 L). The one-year term is an assumption for any deposit whose renewal term differs until its statement arrives.
Tests: `tests/test_deposit_alert_align_db.py::Renewal`.
