# ADR-0044: Holdings are matched to goals from measured market data and the goals' risk budgets

**Status:** Accepted
**Date:** 2026-10

## Context
Holdings were tagged to goals by a fixed list (`ISIN_GOAL_MAP`, name patterns) written in May 2026: SGBs and small caps to marriage, ELSS to health, mid caps to education. It never looked at a goal's date, how funded it was, what the
fund actually holds, or the market; it ignored FDs and SGBs outside the broker; and it could not tell a tag the owner set from one it had set itself. A first rewrite used asset buckets from a recommended-mix table (5% gold, 75% equity), which
was still the planner's opinion, not the market's. The owner asked that money already held count toward the goal it suits, matched from the market (category, what a fund contains, how it behaves), automatically and continuously.

## Decision
- **Profiles from data** (`app/planning/funds.py`): SEBI category per scheme (AMFI NAVAll.txt), contents by return-based style analysis against real index series (Nifty 50 / Midcap 150 / Smallcap 250 / gold / gilts) with a gentle pull toward the
  SEBI-mandated composition, beta and own volatility for direct shares (Yahoo, via ISIN), debt for FDs/PPF/bonds, gold for SGBs. Each source degrades to the next.
- **Risk budgets** (`app/planning/risk.py`): a portfolio's 1-in-20 horizon h = (1.645σ/μ)² from measured volatility and the planner's derived expected return; a goal may hold what needs no more years than it has. The same calculation gives the safe equity
  share by horizon, replacing the tabled glide path (kept as fallback) in the goal plans and recommendations.
- **Matching** (`app/planning/matching.py`): furthest-from-funded goal among those whose budget the holding fits; cash-like holdings fill the emergency gap first; moved only when the current goal wants it under half as much as the best.
- **Provenance**: `holdings.goal_tag_source` and `manual_assets.goal_tag_source` (`manual` | `auto` | NULL). Setting a tag in the UI writes `manual`; clearing it releases the holding. NULL with a tag equal to the old fixed rule counts as automatic. The source is
  carried to new snapshot rows (`inherit_tags`, the roll-forward) and exposed by the holdings API and the `latest_holdings_deduped` view.
- **Triggers**: after each sync for untagged holdings (`auto_tag_holdings`), the daily sweep for all automatic ones, `POST /api/accounts/holdings/auto-tag` (`dry_run` lists the moves), and when a tag is cleared. With no market data nothing moves.
- Not used: fund-level flows and investor mix (no open per-fund source; category flows only in AMFI's PDF) and momentum (it follows the market, it does not fit a goal). A fund's list of shares has no common machine-readable source.

## Consequences
On the owner's holdings (21 Oct 2026, backup `pre_goal_matching_20261021`) the measured profiles are plausible (a large-cap fund 84% large; a PPFAS flexi-cap fund 15% debt, 51% large; mid-cap funds 55-58% mid, 14-24% small, 18-27% large; a Next-50 index fund half large, half mid/small) and every goal's holdings fit its budget, so
the market-based run moves nothing further; the earlier bucket-based run's ten moves (gold bonds out of Child Marriage, FDs placed) stand and are consistent with it. Retirement's holdings need only about 2 years of a 15-year budget (mostly PPF and FDs): a
reason for the recommendations to suggest more equity there. A bug found on the way: Yahoo stamps a monthly bar 00:00 India time on its first day, so reading it in UTC shifted every month back by one (stock betas near 0); dates are read in India time.
Tests: `tests/test_planning_funds.py`, `tests/test_planning_matching.py`. Limits in docs/PLANNING.md §6.

## Addendum (2026-10-21): the fund's own portfolio, and silver
The first version read a fund's contents from its returns only. Funds publish their holdings monthly; Groww's public fund page carries the latest disclosure (every share, sector, weight) and AMFI publishes the official Large/Mid/Small list by ISIN every half year, so
`funds.holdings_exposure` now places each share and the regression is the fallback and cross-check (an ETF is not on Groww: regression only). On the owner's funds: ICICI Large Cap 86% large, HDFC ELSS 80% large, Mid-cap funds 66-72% mid with 6-13% small, Large & Mid Cap 29/38/29,
PPFAS 66% large with 28% debt and cash. Silver became its own factor (it had been read as gold; its volatility is about 34% a year against 16%): silver ETFs fit no goal (1-in-20 horizon about 65 years) and are reported as a bet. Also fixed: Yahoo's `range=max` thins daily data to monthly
(use period1/period2), futures months with no bar, and gold-ETF unit splits (a month that doubles a NAV is dropped). Groww's endpoint is public but not a published API: it can change, so every use is optional and the output says which source stood (`source`).
