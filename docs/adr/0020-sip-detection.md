# ADR-0020: SIP detection — cadence + amount-stability + ACH-mandate sweep

**Status:** Accepted
**Date:** 2026-06

## Context

`_recent_investing_rate()` started life as `(sum of all investing-account debits over 6 months) / 6`. That under-counted recent-starts and over-counted lumpsums:

- A one-off ₹50,000 fund buy showed up as a "₹8,333/mo SIP" by averaging.
- 11 discretionary Silver ETF buys totaling ₹3.2L showed up as a "₹53k/mo SIP" — they're price-driven discretionary purchases, not a recurring instruction.
- A real ₹20,000/mo SIP that started 3 months ago showed up as "₹10k/mo" (₹60k ÷ 6).
- An owner whose Coin emails aren't ingested showed ₹0 SIP even though their bank had a monthly ₹17k NACH mandate visible on the statement.

The downstream effect: goal projections lied. "On-track" flipped wrong. The user lost trust.

## Decision

Detect SIPs by *shape*, not by where the data lives.

### 1. Cadence + amount-stability filter
Group broker-account debits by `(account_id, counterparty || description)`. A group qualifies as a recurring SIP only when:

- `n ≥ max(3, lookback_months / 2)` — at least 3 hits in 6 months (or half the lookback for any other window).
- `stddev(amount) / avg(amount) < 0.15` — the per-txn amount is tight (CV < 15%). Real SIPs are set in fixed rupee amounts; discretionary buys ride NAV/price and vary by 30%+.

If both pass, the contribution to monthly investing rate is `avg(amount)` — the per-txn average, **not** `sum / lookback_months`. Reason: the per-txn average IS the current monthly SIP rate. Dividing by the window dilutes SIPs that started recently.

### 2. Amount snap to nearest ₹100
Coin / AMC debits typically shave a paisa or two due to NAV rounding (₹19,999 instead of ₹20,000). Snap detected SIP amounts to the nearest ₹100 so reported totals match the user's mental model.

### 3. Bank-statement ACH-mandate sweep
For owners whose broker-side data isn't ingested (e.g. a Coin account on Yahoo mail where statements get caught in Bulk Mail), the SIP still shows up on the bank statement as a NACH debit. Sweep bank-account debits whose description matches:

- `YESB00709000028661` — Coin's specific NSE clearing virtual account at Yes Bank
- `ZERODHA BROKIN`
- `INDIAN CLEARIN` — NSE clearing house mandate name

Apply the same cadence + CV filter to these grouped by amount (each distinct SIP amount is a separate mandate). Sum the per-mandate averages into the monthly rate.

### 4. Per-goal attribution
A detected SIP is attributed to a goal when the underlying instrument's `goal_tag` matches the goal's `goal_type`:
- Broker side: `Transaction.isin → Holding.isin → Holding.goal_tag`.
- Manual side: `ManualAssetContribution → ManualAsset.goal_tag`.

Bank-ACH mandates have no instrument-level info, so they contribute to the family pool. Any family-level SIP that isn't attributed to a specific goal is spread across goals in proportion to each goal's `monthly_sip_needed` — so the rupee never gets silently dropped, but it also doesn't double-count against tagged flows.

## Consequences

- ✅ Projections now reflect the user's *current* monthly SIP rate, not a trailing 6-month average diluted by ramp-up.
- ✅ One-off lumpsums and discretionary stock buys no longer inflate "monthly investing".
- ✅ An owner with an unparseable broker mail still gets their bank-mandate SIPs counted.
- ✅ Reported rupee totals match the user's bank statement to the rupee (paisa rounding absorbed).
- ❌ CV threshold (15%) is a heuristic. A SIP that the user manually variabilises (e.g. step-up of 10%/yr in real time) might fall out. Acceptable — that case is rare and the user can move the SIP into a goal manually.
- ❌ ACH-mandate description patterns are India-specific and Zerodha/Coin-specific. Adding another broker means adding another pattern. Documented inline in `_recent_investing_rate`.
- ❌ A short pause (1–2 months) leaves the SIP still counted at its full amount. A 3+ month pause drops the SIP out of the window naturally. This is good default behaviour but means a 2-month pause sits in a grey zone. Acceptable; we can add an explicit "last-txn > 45 days" pause flag later if it matters.
