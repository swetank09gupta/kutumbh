# ADR-0049: UI audit: one place for each number, native pages, honest freshness

**Status:** Accepted
**Date:** 2026-10-24

## Context
A page-by-page review found the same number shown two or three times, some of it contradicting the cards: Grafana panels on Transactions, Investments and Goals (a goal table with the old typed ₹10 cr target and "On track: No" beside a 98% card, a single-slice donut, a portfolio total of ₹45 L beside ₹93 L), cash flow charted three times, allocation twice, a SIP projections
table repeating the goal cards, and the recommendations' history listing retired rules as "resolved". Net worth history carried days computed under older rules (the co-owner's share of the loan was taken from the wrong borrower's letters; days before the ownership change counted the whole flat). The Accounts page said nothing about
how current each account is, and the Sync page said "no sync has run" although the app updates itself.

## Decision
- **Pages are native; Grafana only for history on Overview** (net worth over time; financial assets over time, the home left out so the lines can be read). Removed: the Cash Flow, Investments and Goal embeds, the Overview cash-flow chart and Grafana allocation (one native "Where the money is" donut, financial assets only), the SIP projections table (its return and median outcome moved into each goal card's "How this was worked out"),
  the "Investments / Savings ₹0" tile, the "repo rate" line in Coming up (the home-loan card carries the forecast), "Other Assets" renamed "EPF, NPS & other".
- **Accounts** shows balance today, statement-through and alerts-through dates and a state (Current / Statement due / Dormant); **Sync** says that updates run by themselves, shows the last automatic run, and tucks period choice and re-processing behind toggles. **Transactions**: narrations reduced to the payee (full text on hover), friendly category names, and the
  "since the last statement" panel excludes transfers to your own accounts and the broker from out/in and says so. Holdings show whose they are; types and goal tags are readable; Manual Assets sorted by type, owner and maturity; closed deposits behind a toggle.
- **Data**: the allocation recommendation (a fixed-share comparison) is removed; the ELSS note fires only if ELSS was bought this year; today's recommendation snapshot drops rules that no longer fire and history of retired rules is not shown as "resolved"; P2P-lending repayments are transfers, SMS-alert charges are bank charges; an alert naming the household itself is a transfer in either direction;
  a forced snapshot rebuild covers every stored day; historical snapshots take the loan principal from the borrower's letters whichever co-owner is being added (days that showed ₹105 L of liabilities against ₹70 L now agree).

## Consequences
Fewer cards, one definition per figure. Net worth history is smooth (≈₹3.5 cr now, ₹3.2 cr in September). Bonds (demat) and SGB (outside the demat) stay two return tiles: the invariants compare them with the Investments table separately.

## Addendum (2026-10-24): history depth and the flat
Net worth history starts on 31 Mar 2021, the first dated holdings statement; bank balances go back to May 2019 and trades to 2018, but without dated holdings and prices there is no honest earlier value (rebuilding it from trades, NAV and share-price history is possible and not built).
The flat counted in history from the beginning (it had no start date) while its loan began on 15 Jan 2024, which made 2021-23 look ₹1.6 cr richer and produced a fake drop in January 2024. A flat that comes with a home loan is now owned from the loan's start date; before it the chart shows the true, smaller figure, and the purchase appears as a step (its down payment came from outside the tracked accounts).
EPF/NPS before their anchor date still show the anchor balance (no dated evidence), so early years are overstated by that part. The Overview chips end with **All**, reaching back to the first stored day.
