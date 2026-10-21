# ADR-0046: Audit: the emergency fund is cash and deposits; one corpus definition; fixed rules retired

**Status:** Accepted
**Date:** 2026-10-21

## Context
A walk through every page found things that no longer matched the planner: the Goals banner still said "4.7% for every goal, override" (inflation is derived per goal since ADR-0043); goal corpus ignored FDs and SGBs the matcher had tagged
to a goal (health, education and marriage were short by those amounts, while the planner's own holdings list counted them); FDs were shared out among goals although the owner treats cash and deposits as the emergency fund; recommendations repeated the goal cards
("Goal at Risk" ×5) and carried fixed targets (large cap 25-30%, international 5-10%, a curated fund list, a 40-year-old P/E anchor); a blend of past XIRR capped by a table and floored at 4% still fed any goal the planner does not cover.

## Decision
- **Emergency fund = bank balances + every live FD and RD**, nothing else (`refresh_corpus_from_networth`: `bank + fd` from the snapshot). It is not matched: `matching.deposits_to_emergency` tags every FD/RD `emergency` (a tag you set yourself stays), the matcher skips deposits and the emergency goal, and a holding left tagged `emergency` is released to the goals. Everything else (stocks, funds, bonds, gold, PPF, EPF) is matched to the other goals from market data. The card says *covered* when the fund is at or above its derived target. The `is_liquid` flag is unused (column kept, checkbox removed).
- **One corpus definition**: a goal's corpus is `goal_plan.holdings_by_owner` for its tag (holdings and manual assets), the same list the planner uses; `consistency.goal_corpus_matches_holdings` checks all goals against it. The plan cache key now includes what is held, not only how much.
- **Retirement is one tag**: `wife_retirement` folded into `retirement` (startup migration); `goal_tags` returns the goal's type only.
- **Retired fixed rules**: the inflation override (UI, `PUT/DELETE /api/goals/settings/inflation`, `set_inflation`); the blended-XIRR return with category caps and the 4% floor (`goal_returns.compute_blended_return`, `CATEGORY_CAPS_PCT`, `DEFAULT_RETURN_PCT`): a goal the planner does not cover uses the return typed on it (`return_source: typed`); the recommendation checks goal shortfall, midcap, international gap, direct-equity scatter and market timing (with `_FUND_SHORTLIST`). Allocation, speculative (silver), emergency, EMI, PPF, 80C (old regime) and the typed-retirement check remain; they read derived figures.
- Manual Assets page: the home loan's end comes from the lender's schedule (as the Overview card), a home has no maturity, closed deposits are behind a "Show closed" link; holdings show AMFI's scheme name for a fund ISIN instead of the statement's abbreviation; goal notes sit under "How this was worked out".

## Correction (same day): SBI's alert balance counted the deposits twice
SBI's "available balance" includes the owner's sweep/MOD deposits. Against the statements it sat above the account balance by exactly 11,48,579 (user) and 5,12,284 (wife) on every date, equal to the sum of their SBI deposits then listed, which are Manual Assets of their own. Net worth took the print as the bank balance
and added the FDs again (bank ₹19.87 L, family net worth ₹3.25 cr). `alert_recon.alert_balance` now subtracts the owner's live SBI deposits (a matured or closed one is in savings); HDFC and Axis alerts print the account's own balance and are untouched. Test: `tests/test_alert_balance_sbi_db.py`.
Result: bank ₹4.66 L, net worth ₹3.10 cr, emergency fund ₹21.14 L against a derived ₹33.91 L (short ₹12.8 L). Open evidence: the user's SBI print fell ₹4 L between 30 Sep and 11 Oct with no matching debit alert (FDs closed or matured; the 30 Sep statement still lists them): the next statement settles it.
Removed on the owner's instruction: the empty HDFC accounts 4841, 2414, 4935. The wife's CDSL "unknown" account was kept: it holds 47 demat movements (quantity evidence for her holdings). The wife's SBI 1636 account row is the same account as 9132 (its alerts matched 9132's statement rows) and has no rows of its own.

## Consequences
(Before the correction: emergency fund ₹36.35 L against a derived ₹33.91 L.) Health corpus ₹14.07 L (was ₹10.29 L: the SGB counted now), its chance 11%; education/marriage SIPs moved slightly (their FDs left). The removed recommendations are not replaced by fixed ones: a derived diversification insight (factor exposure against each goal's horizon budget, from `risk.py`) is on the roadmap. Revisits ADR-0019 (blend and override) and ADR-0044 (emergency priority pass).
Four accounts with no rows of any kind (HDFC 4841, 2414, 4935; wife's CDSL "unknown") remain until the owner approves deleting them.
