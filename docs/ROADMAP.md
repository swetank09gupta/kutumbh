# Plan — build on the rules, in order

Written 12 Oct 2026, after collecting the public rules in [INSTRUMENTS.md](INSTRUMENTS.md). Nothing below is built yet. Each phase is **rules → tests → code → check before/after → docs**, finished before the next starts. New requests go into the Inbox at the bottom and are triaged here instead of being coded on the spot (a bug in shipped behaviour is the only exception).

## Phase 0 — a rules registry (foundation, no visible number changes) — DONE 12 Oct 2026
Public parameters stop living as constants scattered through the code. `app/knowledge/rules.yml`, effective-dated, each entry with `source`, `effective_from`, `verified_on`:
PPF limits and dates · TDS thresholds (₹40k/₹50k → ₹50k/₹1L on 1 Apr 2025) · capital-gains rates and holding periods (STCG 15%→20%, LTCG 10%→12.5% on 23 Jul 2024; ₹1L→₹1.25L) · SGB terms and the 1 Apr 2026 exemption change · MF stamp duty · equity charges · EPF rate · repo rate.
Live feeds stay where one exists (NSI PPF table, AMFI/mfapi, World Bank); the rest are verified by hand with a **staleness guard** (consistency warning when `verified_on` is over 120 days old, or a quarter-start has passed).
*Built:* PPF limit, 80C cap, TDS, capital gains, MF stamp duty, EPF, repo (SGB and equity charges join when their phase needs them); PPF/80C code reads it; `public_rules_are_current` warns each quarter; no figure moved. *Acceptance was:* a test per rule (source and dates present, effective ranges do not overlap, lookup by date returns the right value); the existing PPF/TDS/CPI code reads the registry; `check.sh --live` shows no figure moved.

## Phase 1 — a rules-and-deadlines calendar — DONE 12 Oct 2026
*Built* (`/api/deadlines`, Overview card): PPF next 5th-of-month, FY headroom, FY end and maturity (only where the opening date is known) · deposit maturities (and matured-but-still-listed) · ELSS unlocks (3 years per instalment, FIFO) · SGB maturity · repo change against the loan letter. Each item cites a registry rule or its evidence; the acceptance guard is `deadlines_cite_a_basis`.
*Moved:* TDS-threshold tracking → Phase 4 (needs interest per payer per year); loan reset date and EMI forecast → Phase 2 (the lender's reset date is not in its letters).

**SGB early exit: not wanted.** You will hold to maturity, so neither an early-exit comparison nor exit windows are built (decided 12 Oct 2026); only the maturity date is shown.

## Automation (done 12 Oct 2026, between Phases 1 and 2)
Alerts, loan letters and the RBI repo rate now update by themselves through one pipeline (ADR-0029); a warning shows if it stops. Still hand-verified, with the quarterly warning: tax thresholds, PPF limit, EPF rate (no official machine-readable page). Still no source: EPF balance, the flat's value, the wife's unrecorded PPF deposit.

## Phase 2 — home loan, fully reconciled — DONE 12 Oct 2026
Amortisation schedule built from each lender letter (rate, EMI, EMIs left) · every EMI debit checked against the schedule (principal/interest split, EMIs-left count) · payoff date and **interest saved by each prepayment** · **rate forecast**: the loan has passed the repo through 1:1, so after the 7 Oct 2026 hike expect ≈ 7.35% at the next reset (shown as a forecast, replaced by the next letter) · old-regime 24(b)/80C use per co-borrower share.
*Built* (`/api/loan`, Home loan card, ADR-0030): reconciliation of every letter interval (10 of 12 reproduce to the rupee; two older ones are shown with their size), payoff month, interest saved per prepayment, forecast 7.10% → 7.35% (repo + 1.85%, labelled), 24(b)/80C per share and regime (you old, wife new), check `loan_reconciles_with_lender`. Found: the lender applies its letter EMI, the debit can lag one instalment.

## Phase 3 — SGB completeness — DONE 12 Oct 2026
*Built* (`/api/sgb`, Gold bonds card, ADR-0031): tranche registry (fixed the wife's ISIN, a different series), coupons matched to the paisa and counted (14 for you, 10 for the wife), cost = what the bank shows was paid (your Series III: ₹5,359 a gram, the online price), next coupons on the deadlines card, check `sgb_coupons_reconcile`. **Open for you:** the wife's coupons of ₹1,590.94 come from a 25 g bond (2022-23 Series I, ≈ ₹3.9 L) that no statement lists; add it in Manual Assets if it is hers.

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
- Equity dividends (e.g. ONGC, Vedanta credits) are categorised "other" and are not in returns: same arithmetic idea as SGB coupons, needs the dividend per share.
