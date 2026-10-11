# ADR-0024: One definition per number, one authority per tag, and a guard that proves they agree

**Status:** Accepted
**Date:** 2026-10

## Context

Pages disagreed because each page (and each Grafana tile) computed its own version of the same thing: holdings were summed three ways (net worth, the holdings view, XIRR), "expense" had three filters, the goal cards and the projections table used two calculators, "current SIP" added every debit on investment accounts and split the household total across each person's goals. Share trades and fund redemptions were counted as spending, the same UPI payment printed in two statement layouts was counted twice, and money moved between own accounts was income on one side and spending on the other. It was found by hand every few months.

## Decision

**Holdings** — every consumer reads `latest_holdings_deduped` (net worth, returns, goals, recommendations, Grafana). XIRR counts only the winning row per ISIN, so a stale folio statement cannot value the same fund twice.

**Cash flow** — `v_cashflow` / `v_cashflow_monthly` (`app/semantic/definitions.py`) are the only place income and expense are defined; the API, the advisor, recommendations and Grafana select from them. Bank accounts only.

| flow | rule |
|---|---|
| expense | debit that is not a transfer, an investment or a loan prepayment |
| prepayment | debit tagged emi/loan_prepayment, shown separately |
| income | credit tagged income (salary, interest/dividend, refund) or a merchant refund |
| investment | tagged investment, buys and redemptions |
| transfer | own-account moves, wallet top-ups, demat unit counts |
| other_credit | every other credit: not income until tagged |

**Transactions** (`app/txn/`)
- *Duplicates*: same account, date, amount and direction **and** a shared UTR prefix. Separate payments the same day have different UTRs and are kept.
- *Own transfers*: equal amount, ≤1 day, different accounts, unique on both sides; or a counterparty carrying a household name (covers accounts whose statements we do not hold).
- *Authority order for a tag*: trade → user → paired → rule → remembered payee. Re-tagging never overwrites `user`, `paired` or `trade`.
- *Payees nobody has labelled* go to a Review list; your label is remembered and applied to past and future payments. A local model (qwen2.5:7b) can pre-fill suggestions on demand, but **never applies a tag itself**: the 3B model labelled people as "shopping" at 0.9 confidence and even the 7B makes confident errors.

**Goals** — one calculator (`project_all_goals`). Current SIP comes from the trade ledger: a fund bought in at least half of the last 6 months, or in each of the last two, valued at the median month. A tagged SIP funds that goal only; an untagged SIP is split across goals its owner can fund. The emergency fund is a floor, not a dated target. The stored SIP column, the cards, the table and Grafana all read this projection.

**Returns** — profit includes sale proceeds; XIRR/gain/CAGR are withheld (with a reason) for any class, goal or fund where purchase history is incomplete (`cost_basis_quality = partial`) instead of being computed from a partial cashflow stream.

**Guard** — `app/consistency.py` runs after every sync and at `GET /api/health/consistency` (banner in the UI): Family = Self + Spouse, net worth = holdings, returns = holdings, goal SIP stored = projected, expense totals shared, no duplicate bank rows, no unpaired own transfers, broker rows are investments, no assets on archived owners (fail = a bug); ledger errors, stale bank balances, unreviewed payees (warn = a data gap).

## Consequences

- ✅ A number has one definition; changing it is one edit and the guard shows if anything still disagrees.
- ✅ Tagging mistakes are corrected once, and survive re-processing.
- ❌ "Withheld" appears where the data genuinely cannot support a number (pre-ledger purchases, missing Coin mails) until a tradebook / holdings export fills the history.
- ❌ UPI payments to individuals stay `other` until labelled; the Review list makes that a few minutes a month, not a guess.
