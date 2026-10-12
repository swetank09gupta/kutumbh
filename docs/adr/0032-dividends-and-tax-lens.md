# ADR-0032: Dividends are proven from the stocks held; the tax lens reads proven data and the verified rules

**Status:** Accepted
**Date:** 2026-10

## Context
Equity dividends (32 credits from 15 companies for you, a few for the wife) sat in "other" with no ISIN, so they were in no return; and nothing summarised what is taxable: realised gains, interest against the TDS thresholds, dividends, coupons.

## Decision
**Dividends** (`app/ledger/dividends.py`). A bank credit is a dividend when *shares held before the ex-date × per-share amount* equals it to the paisa (or 90% of it after TDS), within 75 days after the ex-date. Ex-dates and per-share amounts are public (Yahoo Finance), fetched once a week per traded stock into `data/cache/dividends.json` by the daily evidence step; `scan()` and `tag()` never use the network. Shares held = the ledger's own trades with trade date before the ex-date (so a purchase on the ex-date is not entitled). Several candidates are separated by the narration (the truncated payer name); if that does not decide, the credit is reported as ambiguous, not guessed. Matched credits become income (`dividend`) carrying the stock's ISIN, so the one returns model counts them as flows of that stock. 32 of 38 dividend-looking credits matched; the 6 that did not (no history for a stock, shares from before the trade history) are listed.
**Tax lens** (`tax_gains.py`, `tax_view.py`, `/api/tax`, a Tax card on Overview):
- Gains: FIFO lots from the ledger's effective trades; long-term after more than 12 months; STCG/LTCG rates and the LTCG exemption by **sale date** from the registry (the 23 Jul 2024 change), short-term loss set off against long-term gain, long-term loss carried forward, never against short-term gain. Tax class from AMFI's own scheme category (a silver ETF is recognised by name: AMFI files it under "Equity ETF"). Gold/silver ETFs sold from 23 Jul 2024 at 12.5% with no exemption; earlier sales and debt funds bought before Apr 2023 are reported as not computed; unknown fund types are never guessed. Sales of units with no purchase on file are named and left out.
- Income per financial year: savings interest (80TTA, old regime only), deposit interest grossed up by the TDS notices, dividends, SGB coupons; shown as amounts at slab.
- TDS: deposit interest per bank against the s.194A threshold of that year (no check where the registry has no verified threshold); when the bank has deducted TDS although credited interest is below the threshold, accrued uncredited interest is the reason and it is said so. Dividend per company against the s.194 threshold.
- Unrealised: the tax if everything held were sold today, with the exemption left this year.
- Not computed on purpose: the slab tax itself (total income is not in the system), brokerage/stamp duty netting (trade prices are gross), corporate actions.

## Found on the way
Five ₹100,000 **debits** in the wife's Axis account are labelled interest income because the bank's statement text mentions interest; the cashflow counts them as ₹5 lakh of expenses. They are reported (`/api/tax` issues), not corrected: only the owner knows they are transfers to her own deposits. A rule that starts on its verification day cannot serve past years: 80TTA was verified back to AY 2013-14 so the filing year resolves.

## Consequences
New rules: holding period, listed-other LTCG rate, dividend TDS threshold and rate, 80TTA cap. AMFI cache keeps categories. One figure moved (the retirement goal's SIP needed, from the higher blended return with dividends). Tests: `tests/test_tax_gains.py` (hand-calculated), `tests/test_dividends_tax_db.py`.
