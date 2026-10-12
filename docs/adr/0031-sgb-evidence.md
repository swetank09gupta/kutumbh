# ADR-0031: Gold bonds are proven from the bank statements: tranche facts, coupons, what was paid

**Status:** Accepted
**Date:** 2026-10

## Context
SGB cost came from a hard-coded tranche table and units were assumed to cost the nominal price from the issue date; coupons were not in the return. Checking real credits against public prices showed:
- **One ISIN was the wrong tranche.** IN0020230069 is 2023-24 *Series I* (27 Jun 2023, ₹5,926), not the September series at ₹5,873. The wife's SGB cost basis and date were wrong.
- **Coupons exist and were invisible.** Your 14 coupons arrive as "NEFT … CENTRAL DEPOSITO(RY)" credits (the narration is truncated in newer statements) and the wife's as CDSL and RBI "Sovereign Gold B" credits, all categorised as transfers. Each equals units × nominal × 1.25% to the paisa.
- **Online applicants paid ₹50 a gram less.** Your Dec 2022 debit "SGBS 25 GMS" is ₹133,975 = 25 × (₹5,409 − ₹50); the ledger charged ₹135,225.
- **A bond no statement lists.** The wife receives ₹1,590.94 every six months from RBI Mumbai = 25 g × ₹5,091 × 1.25% (2022-23 Series I, coupons 28 Jun / 28 Dec).

## Decision
- `app/knowledge/sgb_tranches.yml`: verified tranches (ISIN, series, issue and maturity date, nominal, online price, source). A tranche is added only when a holding or coupon stream needs it; an unknown tranche is unknown, never guessed from its name.
- `app/ledger/sgb_evidence.py` finds, by arithmetic, (a) coupons: a bank credit equal to units × nominal × 1.25% for a known tranche within 6 days of a coupon date; (b) the subscription debit: SGB in the narration, near the issue date, equal to units × nominal or × online price. Units come from the depository statement (the ledger's own input) or a Manual Asset.
- The ledger costs the first subscribed units at what was paid, on the day it was paid; without that evidence the nominal price from the issue date, flagged `issue_price_cost`.
- `tag()` (run by the one categorisation authority, `txn.pipeline.retag`) marks matched coupons as income (`sgb_coupon`) and ISIN-tags them **only when the bond is held**, so they enter the one returns model as flows; the subscription debit gets its ISIN. A bond no statement lists is never half-counted: its coupons are income only until it is added in Manual Assets.
- A coupon is "missing" only when it is due after the last matched one, 10 days old, and the owner's statements reach past it.
- `GET /api/sgb`, a Gold bonds card, deadline items for the next coupon of each bond, check `sgb_coupons_reconcile` (warning).

## Consequences
Only three figures moved (bonds XIRR: yours up from the lower cost and the coupons, the wife's down from the corrected cost and an earlier issue date). Coupons moved from "transfer" to income (a few thousand rupees a year). Whether the wife holds the 2022-23 Series I bond is for her to confirm: it is not added to net worth automatically. Tests: `tests/test_sgb.py`, `tests/test_sgb_evidence_db.py` (three mutations checked).

## Addendum: a bond held by hand (12 Oct 2026)
The owner confirmed the wife holds the 2022-23 Series I bond outside the demat (RBI/bank), so it is in no statement except its coupons. It is a Manual Asset (type SGB, 25 g, issue date and nominal price assumed and labelled). Because such a bond has no statement price:
- its value is the statement price of the held SGB with the **nearest maturity**, set by the pipeline (`hand_held_sgb_value`) and noted on the asset; the gold-spot refresh skips known tranches (spot is ~14% below SGB market prices);
- its coupons are return flows (Investments page and Manual Assets page share `_manual_asset_cashflows`) and its coupon and maturity dates are on the deadlines card;
- a Manual Asset is identified by its **name** (or an ISIN); notes are read only if the name names no tranche, because a note may mention another series.
Purchase and redemption: the purchase is not in any statement we hold (her SBI statements for Jun 2022, Dec 2022, Jun 2023 and Dec 2023 are missing; those are also the coupon dates not seen), so cost and date stay at the nominal price and issue date; redemption is at maturity, 28 Jun 2030, at the IBJA price.
