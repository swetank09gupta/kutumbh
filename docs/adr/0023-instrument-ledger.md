# ADR-0023: Instrument ledger — trades for cost and dates, statements for quantity, reconciliation between them

**Status:** Accepted
**Date:** 2026-10

## Context

Holdings came only from monthly statements, so positions were up to a month stale and carried no cost (the statement prints a month-end price; the app stored it as both cost and price, so P&L was 0%). Meanwhile every trade was already arriving by e-mail (contract notes, Coin allotments). Reading both as cashflows had caused duplicates before (ADR-0015/0022), which is why contract notes were once skipped.

## Decision

Keep three kinds of fact separate and never add one to another:

| Fact | Source | Table | Gives |
|---|---|---|---|
| Trades | contract notes, Coin, tradebook | `instrument_trades` | exact date, qty, price, cost |
| Movement lines | monthly demat statement | `demat_movements` | evidence + classification only |
| Balances | monthly demat statement (Zerodha) and CDSL CAS | `statement_balances` | quantity truth at each month-end |
| Alerts | CDSL "Transactions In Your Demat Account" e-mails | `demat_movements` | units that arrived after the last statement, within a day |

**Quantity truth is the statement balance.** Between two consecutive balances the trades that settle in the interval must add up to the change. A non-zero residual is a finding (missing trade, duplicate, transfer, corporate action), never absorbed silently. Movement lines are not used for the arithmetic: Zerodha's "Early Pay-in" lines list a debit and its reversal while the balance doesn't move, and some lines are missing. The parser therefore records a movement as the change in the Balance column.

**Position now** = newest balance + trades settling after it + CDSL alerts after it that no trade accounts for (a pending trade and its alert are matched, not added twice; alert-only units carry unknown cost and raise `alert_without_trade`). This is what covers a mailbox that never receives Coin mails — the wife's Yahoo gets the CDSL alerts but no Coin mails. Cost is a weighted average over trades, with units we cannot trace (held before our history, SGB, transfers) tracked separately. `cost_basis_quality` is `exact` only when every unit is traced; otherwise invested/P&L are left empty rather than guessed.

**ISIN aliases** (AMFI lists two ISINs per scheme and its name lookup can pick the wrong one) are learned per owner from exact fractional-unit matches against demat lines and applied to trade rows only; statement and demat ISINs are the depository's and are never rewritten. An ISIN the owner's depository has shown is never treated as an alias of theirs (it may be an alias for another owner: Motilal's `…01999` is the wife's real Large & Midcap fund and a lookup mistake for the user's Midcap trades).

**De-duplication** is by construction, not clean-up: each row has a `natural_key` (`ON CONFLICT DO NOTHING`), and where two sources report the same (ISIN, date, side) only the best-ranked counts (contract note/Coin > tradebook). Same totals → the lower rank is superseded and `POST /api/ledger/dedupe` removes it and its cashflow; different totals → `source_conflict`, nothing deleted.

**Settlement** uses the date the contract note prints; else T+1 business day for exchange trades; Coin units credit on the allotment date.

Roll-forward writes `holdings` rows (`source_file='ledger:rollforward'`, today's date) so every consumer — API, net worth, XIRR, goals, the dedup view — sees current positions unchanged. Statement rows are never modified. Prices: AMFI NAV for funds/ETFs, Yahoo (NSE ticker from the contract note) for equities, falling back to last trade then statement rate, with `price_source` recorded.

## Reconciliation (`recon_issues`, `/api/ledger/*`)

Re-derived on every run; issues that stop being detected are marked resolved.

| Kind | Severity | Meaning |
|---|---|---|
| `missing_trade` | error | balance rose, a settlement line exists, no contract note/Coin trade |
| `trades_exceed_statement` | error | trades add up to more than the balance moved — duplicate or wrong date/qty |
| `off_market_transfer`, `corporate_action`, `untraced_change` | warn/info | balance moved with no trade; cost unknown |
| `source_conflict` | warn | two sources disagree on a day's quantity |
| `depository_mismatch` | warn | Zerodha statement vs CDSL CAS differ on the same date |
| `missing_statement` | warn | >35 days between statements |
| `unresolved_instrument` | warn | trade with no ISIN (not counted) |
| `trade_without_cashflow` / `cashflow_without_trade` | warn | ledger and `transactions` disagree (XIRR input) |
| `alert_without_trade` | warn | CDSL alert after the last statement, no trade on file; counted, cost unknown |
| `settlement_boundary` | info | +q and −q in adjacent months cancel: a trade settled across a month-end |
| `pre_history_mismatch` | warn | trades before the first statement exceed it: sells before our history |
| `pending_trade` | info | settles after the last statement; counted until confirmed |

`POST /api/ledger/verify` takes quantities copied from Zerodha Console and diffs them against the ledger.

## Consequences

- ✅ Holdings are current within a day of a trade; cost and first-buy date are exact where traced.
- ✅ Every statement independently checks the ledger; gaps are listed, not hidden.
- ✅ Same code path for live sync, scheduled sync and reprocess; idempotent.
- ❌ History before the earliest contract note/Coin e-mail stays `partial` until a tradebook CSV or Console holdings export supplies it.
- ❌ Exchange holidays are not modelled in T+1; a boundary trade can show as pending for a month, visible in the reconciliation.
- ❌ Corporate actions are not auto-adjusted; they surface as `corporate_action`/`untraced_change`.


## Addendum (ADR-0026)

Units the depository shows with no trade are no longer left as unknown cost when the evidence exists: `app/ledger/derive.py` turns them into `derived` trades (units/date from the line, price from the AMFI NAV, cash from the bank debit). Source rank `derived` is lowest and the rows are rebuilt on every refresh.
