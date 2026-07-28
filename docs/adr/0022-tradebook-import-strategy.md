# ADR-0022: Tradebook import strategy — exact dates over AGTS annual aggregates

**Status:** Accepted
**Date:** 2026-07

## Context

Zerodha provides two historical data formats for equity trades:

1. **AGTS (Annual Global Trading Statement)** — per fiscal year, one row per (ISIN, exchange, segment) with total buy qty/value and total sell qty/value. No individual trade dates.
2. **Tradebook CSV** — individual trades with exact date, Trade ID, quantity, price. Calendar-year granularity (one file per CY).

For XIRR, date accuracy matters. An AGTS synthetic that places ₹5L of trades on Apr 1 vs the actual Jul–Sep clustering introduces 3–6 months of dating error and can move the per-instrument XIRR by 2–5 percentage points for shorter hold periods.

## Decision

**Prefer exact tradebook data. Import AGTS only when no tradebook exists for that period.**

Priority order for a given (ISIN, period):
1. Email-parsed contract notes (most accurate — exact execution date + price from the exchange confirmation)
2. Tradebook CSV (exact date + Trade ID, but order-level — multiple rows per day)
3. AGTS synthetic (fiscal-year aggregate — last resort)

### When NOT to import a tradebook

Skip a tradebook file if the same period is already covered by email-parsed entries **and the totals match**. Tradebook rows are order-level splits of the same consolidated daily entries that email parsing produces. Importing both would double-count.

Verification query before any tradebook import:

```sql
-- Compare tradebook totals against DB totals for (isin, date, type)
SELECT t.isin, t.date, t.transaction_type,
       SUM(t.quantity) AS db_qty, SUM(t.amount) AS db_amount
FROM transactions t
WHERE t.account_id = '<broker_account_id>'
  AND t.isin = '<isin>'
GROUP BY t.isin, t.date, t.transaction_type;
```

If `db_qty` and `db_amount` match tradebook row sums after grouping, the period is already covered — do not import.

### Handling AGTS / tradebook period overlap

AGTS files use fiscal years (Apr–Mar); tradebook CSVs use calendar years (Jan–Dec). When both exist for overlapping periods, compute the gap:

```
CY gap synthetics = AGTS FY total − tradebook Q1 (Jan–Mar) activity per ISIN
```

Example: AGTS FY2022-23 (Apr 2022 – Mar 2023) + EQ-2023 tradebook (Jan–Dec 2023):
- Jan-Mar 2023 → covered by exact tradebook rows
- Apr-Dec 2022 → computed gap synthetic, dated to nearest known trade window

If the gap = 0 for all instruments, no synthetics are needed (the tradebook fully covered the fiscal year).

### Fingerprint namespaces

Each import source must use a distinct fingerprint prefix to prevent false dedup collisions:

| Source | Fingerprint prefix |
|--------|-------------------|
| EQ tradebook (exact) | `tradebook_eq{year}_{trade_id}` |
| Gap synthetics | `tradebook_gap_{period}_{isin}_{type}` |
| Approximate-date fills | `tradebook_{period}_approx_{sym}_{date}_{qty}_{price}` |
| Email contract notes | `zerodha_{date}_{isin}_{qty}_{price}` |
| AGTS (legacy) | `agts_{fy}_{isin}_{exchange}_{segment}` |

Always use `ON CONFLICT (fingerprint) DO NOTHING` — never update on conflict.

### MF tradebook

Do not import Zerodha Coin MF tradebook. The Coin account is already fully covered by email-parsed transaction statements at the consolidated daily NAV level. MF tradebook rows are per-order slices of the same daily units.

## Consequences

- ✅ Per-instrument XIRR is accurate to the actual trade date rather than a fiscal-year boundary approximation.
- ✅ Dedup is safe across sources: each namespace is mutually exclusive.
- ✅ The approach is incremental: AGTS rows that predate any available tradebook are preserved as lower-confidence data; they are only deleted when exact tradebook data covers the same period.
- ❌ Importing tradebook data is a one-time manual operation (download CSV from Zerodha Console → run SQL). There is no automated sync path for historical tradebook data.
- ❌ Corporate actions (bonus issues, splits, rights) are not in any tradebook or AGTS. Known cases must be documented separately. Current known instance: NYKAA 5:1 bonus Nov 2022 (20 free units, ~₹3,420 untracked cost basis; position fully closed).
