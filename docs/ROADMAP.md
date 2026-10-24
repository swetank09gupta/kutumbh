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
*Built* (`/api/sgb`, Gold bonds card, ADR-0031): tranche registry (fixed the wife's ISIN, a different series), coupons matched to the paisa and counted (14 for you, 10 for the wife), cost = what the bank shows was paid (your Series III: ₹5,359 a gram, the online price), next coupons on the deadlines card, check `sgb_coupons_reconcile`. The wife's 25 g 2022-23 Series I bond (held outside the demat, confirmed) is a Manual Asset valued like the nearest-maturity held SGB; its purchase is in missing statements, so cost/date are assumed.

## Phase 4 — tax lens (rule-driven, no new data) — DONE 12 Oct 2026
*Built* (`/api/tax`, Tax card, ADR-0032; regimes: you old, wife new): dividends proven by arithmetic and counted in returns (32 credits matched, 6 listed as unmatched); realised gains per FY with set-off, exemption and rates by sale date; savings/deposit interest, dividends and SGB coupons per FY; deposit interest per bank vs the TDS threshold; 80TTA (old regime); tax if everything were sold today. Slab tax is estimated per person from credited salary plus proven income, labelled a floor (ADR-0036).

## Phase 5 — EPF (12 Oct: deferred; built 21 Oct 2026 as a shared balance rolled forward, ADR-0045)
**Now:** the balance and this month's contribution are shared in `config/accounts.yml` (`epf`) about once a year; the app keeps them as a Manual Asset, rolls them forward at the declared rate, counts them in net worth and in the retirement goal, and warns after 13 months. The wife's EPF and NPS are unknown (no data); the PF of the owner's two old exempted employers was transferred into his main account, so his balance is the whole of it. *Original check (12 Oct):* looked for an automatic feed: the mailboxes hold only EPFO account-security notices (password and contact changes) and your 2023 mails about transferring the old PF balance; no passbook, balance or payslip mail exists (payslips: none after 2017). The balance needs a UAN login with OTP and captcha, which is not something to automate. Revisit only if a feed appears.

## Phase 6 — known data gaps (worked through 12 Oct 2026)
- **Done:** Axis statements read properly (ADR-0033) and a missing Axis FD found; old contract notes and same-day orders (ADR-0034); restart policies and the bootstrap test fixed; Axis FD growth anchored correctly.
- **Payees (333 groups, 621 small UPI payments, ₹2.1 L of debits over the years):** the local model answers "other" at 0.20 confidence, which is honest; these stay a human job in the Review list. They are already counted as expenses.
- **Union Bank and Axis (corrected 21 Oct 2026):** the senders changed domain in 2026 (Union `ubi.bank.in`, Axis `statements@axis.bank.in`, SBI alerts `alerts.sbi.bank.in`) and are read again: Union 6026 and the wife's Axis 3856 are current to 30 Sep 2026, balances chain, ADR-0038/0040. The user's own Axis (ex-Citi) is dormant at ₹0. An earlier line here said they were stale; that was an old note, not a fact.
- **Locked PDFs — solved (ADR-0035):** Union Bank statements open with the account number (2020-21) or name+DDMM (2024+); `IT_PROV…`/`IT_FINAL…` are HDFC loan tax certificates (password: loan account number) and now feed the loan card and tax view. Still locked: 3 of the wife's old SBI/Axis files.
- **Two newer FDs (…4395, …5054)** are balance-only until they have enough interest credits; they resolve by themselves.
- **Cost unknown on sales:** your LIC (IPO allotment) and Nykaa (bonus) units are now proven from the registrar's mails (ADR-0035). The wife's pre-2020 BEL/HDFC/SBIN/Reliance/Vedanta trades came from her Console tradebooks for 2018, 2020 and 2021 (ADR-0041); she had no trades in 2017 or 2019.

## Self-sustaining audit (12 Oct 2026, ADR-0037)
Updates without a person: statements, alerts, loan letters/certificates, registrar notices (every 6 h, since the last good run); repo rate and 18 rule pages (daily); AMFI NAVs and Yahoo share prices (each rebuild, expiring after 6 h / the day); dividend history; derived trades. Each now has a check that goes to warn when it stops (`prices_are_live`, `rules_are_current`, `automation_is_running`, `bank_balances_chain`, ledger reconciliation).
**Still not live (alerts):** SBI sends no e-mail for UPI payments or mandate (NACH/SIP) debits, so between statements those rows are missing from the Transactions list (the balance is right: it is the bank's own printed balance); the Zerodha payment mails now cover transfers to the broker (21-24 Oct 2026). **Still not live:** SGB prices (monthly statement rate), the flat's value, FD/RD values (accrued from the statement's contract rate, not a feed), statements the banks never mail (SBI wife Aug 2025, SBI user Oct 2024), accounts that send no alerts (Union, Axis: statements only).

## How each phase is run
1. Update the rule section in INSTRUMENTS.md (source, date) and the registry.
2. Write the tests (including a mutation that fails without the change) and any invariant.
3. `scripts/check.sh --live` before; build; after. Every moved figure must be one the phase meant to move.
4. DECISIONS / INVARIANTS / ADR / docs in the same commit; UI check for Family, Self, Spouse.

## Decisions (12 Oct 2026)
1. Order 0 → 1 → 2 → 3 → 4 approved.
2. Tax regime: you **old**, wife **new**.
3. EPF: skipped on 12 Oct; built on 21 Oct as a balance you share (the portal needs a captcha login, so it cannot be fetched).
4. SGB early-exit comparison: not wanted.

## Inbox (requests waiting for triage)
- (Done) The five ₹100,000 "debits" in the wife's Axis account never happened: they were summary text read as transactions. Axis statements are now read by column and reconciled (ADR-0033); a real Axis FD (₹100,000, Oct 2025) was found.
- Dividends: 36 of 38 candidate credits are matched (older history, later bonus/split-adjusted amounts, 120-day window); the Vedanta credit matches since the 2020 tradebook (ADR-0041); the SBI (2021) credit of ₹68 is the one left.
- **Goal return cliff: closed (ADR-0046).** The blend of past XIRR and its 4% floor are gone; planned goals use the market return, custom goals the typed one.
- **Planning (ADR-0042, ADR-0043, docs/PLANNING.md), built 21 Oct 2026:** one goal calculator with derived targets (retirement as one household goal, education abroad staged by the child's age, emergency months),
  per-category live inflation, derived returns, per-person tax, chance and SIP for 80% on the existing goal cards and table, market scenarios in the scenarios window, recommendations reading the same figures, prepay-vs-invest in the loan card.
  **Built 21 Oct (ADR-0045):** EPF, co-owned home in net worth, current pay in the tax slab, family history in the horizon, a return-of-premium plan, capacity and a funding order. **Next:** (a) any pension or EPS for either of them, and NPS valued by NAV (today only contributions; EPF and NPS are in, ADR-0045 addendum); (b) per-scheme AMFI data (average AUM, risk parameters, age-wise folios) if a use is found; (c) a monthly review that re-runs the plan and says what moved; (d) the deterministic path now serves only custom goals and the no-market-data case (ADR-0046); retire it once planning has run reliably for a while; (e) post-retirement spending that changes with age (the surviving-spouse step is built); (f) the primary earner's gross salary (TDS) for the surcharge.
- **CAS reconciliation (24 Oct 2026):** the monthly CDSL CAS (`…_TXN.pdf`, all depository holdings and MFs held in demat; "no MF folios" for both PANs) is the independent check on holdings. Checked by hand for 30 Sep 2026: the wife's total ₹8,78,358 equals the snapshot to the rupee; the user's units equal the ledger's after the October purchases (HDFC Bank 114 + 32, UTI Nifty Next 50 + the 5 Oct SIP). To build: a standing check that compares each CAS to the ledger's units on its date and shows the difference on the Accounts page. KFintech/CAMS send no personal statements any more (only portfolio disclosures and notices), so the KFintech accounts are old data, not a gap.
- **Older net-worth history:** before 31 Mar 2021 (first dated holdings) nothing is shown; rebuild from 2018-19 trades with NAV/price history if wanted. EPF/NPS before their anchor date show the anchor balance.
- **Audit follow-ups (21 Oct 2026, ADR-0046):** (a) a *derived* diversification insight to replace the fixed-target recommendations that were removed (factor exposure against each goal's horizon budget, from `risk.py`); (b) (done) the wife's SBI 1636 row, a duplicate of 9132, was deleted; (c) `recommendation_snapshots` keeps history of retired recommendations (they show once as "resolved"); (d) the wife's EPF/NPS (no data); (e) fund-name tidy-up for stocks (statement names such as "TATA STEEL-EQ1/-").
