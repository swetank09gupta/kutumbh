# Plan — build on the rules, in order

Written 12 Oct 2026, after collecting the public rules in [INSTRUMENTS.md](INSTRUMENTS.md). Nothing below is built yet. Each phase is **rules → tests → code → check before/after → docs**, finished before the next starts. New requests go into the Inbox at the bottom and are triaged here instead of being coded on the spot (a bug in shipped behaviour is the only exception).

## Phase 0 — a rules registry (foundation, no visible number changes)
Public parameters stop living as constants scattered through the code. `app/knowledge/rules.yml`, effective-dated, each entry with `source`, `effective_from`, `verified_on`:
PPF limits and dates · TDS thresholds (₹40k/₹50k → ₹50k/₹1L on 1 Apr 2025) · capital-gains rates and holding periods (STCG 15%→20%, LTCG 10%→12.5% on 23 Jul 2024; ₹1L→₹1.25L) · SGB terms and the 1 Apr 2026 exemption change · MF stamp duty · equity charges · EPF rate · repo rate.
Live feeds stay where one exists (NSI PPF table, AMFI/mfapi, World Bank); the rest are verified by hand with a **staleness guard** (consistency warning when `verified_on` is over 120 days old, or a quarter-start has passed).
*Acceptance:* a test per rule (source and dates present, effective ranges do not overlap, lookup by date returns the right value); the existing PPF/TDS/CPI code reads the registry; `check.sh --live` shows no figure moved.

## Phase 1 — a rules-and-deadlines calendar (most useful, time-sensitive)
One list, each item tied to a rule id: SGB exit windows (RBI calendar, shown as dates only, no early-exit comparison) · PPF: next 5th-of-month, ₹1.5L headroom for the FY, maturity/extension, 7th-year withdrawal · FD/MOD maturities · ELSS unlock dates (3 years per instalment) · loan reset after a repo change · TDS threshold about to be crossed.
*Acceptance:* API + a card; every item cites its rule; fixture tests for each rule; an invariant that no item is shown without a registry rule.
**SGB early exit: not wanted.** You will hold to maturity, so no early-exit tax comparison is built (decided 12 Oct 2026). Exit windows stay as plain dates only.

## Phase 2 — home loan, fully reconciled
Amortisation schedule built from each lender letter (rate, EMI, EMIs left) · every EMI debit checked against the schedule (principal/interest split, EMIs-left count) · payoff date and **interest saved by each prepayment** · **rate forecast**: the loan has passed the repo through 1:1, so after the 7 Oct 2026 hike expect ≈ 7.35% at the next reset (shown as a forecast, replaced by the next letter) · old-regime 24(b)/80C use per co-borrower share.
*Acceptance:* the schedule reproduces every letter's figures (already true to the rupee for the principal); a mismatch is a consistency warning; forecast labelled and tested against the 2024-26 history.

## Phase 3 — SGB completeness
Coupons (the wife's ₹1,590.94 half-yearly NEFT credits from RBI; none found for your two tranches) enter the return; **reconcile each coupon to units × issue price × 1.25%** (the wife's is ₹244 lower than a 25 g holding implies: either the holding or the tranche price is off, to be found); redemption value from the IBJA price (at maturity only).

## Phase 4 — tax lens (rule-driven, no new data)
**Regime per person: you = old, wife = new** (12 Oct 2026). 80C/24(b)/ELSS advice applies to you only; the wife's view uses new-regime slabs and shows no 80C or 24(b) deductions. Realised gains per FY (FIFO, STCG/LTCG by holding period, ₹1.25L LTCG headroom) · unrealised tax per holding · FD/MOD/savings interest per person vs the TDS threshold, 15G/15H eligibility, an interest-income summary for the ITR · all rates from the registry.

## Phase 5 — EPF (deferred)
Skipped for now (12 Oct 2026): no automated source exists, and a manual entry is not wanted. Revisit only if an automated feed appears.

## Phase 6 — known data gaps (housekeeping)
881 payees to label · stale Union Bank / Axis statements · two newer FDs without enough history · the 19 unreadable HDFC PDFs · compose bugs (ollama-init, bootstrap race).

## How each phase is run
1. Update the rule section in INSTRUMENTS.md (source, date) and the registry.
2. Write the tests (including a mutation that fails without the change) and any invariant.
3. `scripts/check.sh --live` before; build; after. Every moved figure must be one the phase meant to move.
4. DECISIONS / INVARIANTS / ADR / docs in the same commit; UI check for Family, Self, Spouse.

## Decisions (12 Oct 2026)
1. Order 0 → 1 → 2 → 3 → 4 approved.
2. Tax regime: you **old**, wife **new**.
3. EPF: skipped for now.
4. SGB early-exit comparison: not wanted.

## Inbox (requests waiting for triage)
- _(empty)_
