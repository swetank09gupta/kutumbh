# ADR-0044: Holdings are matched to goals from the goals' horizons and gaps

**Status:** Accepted
**Date:** 2026-10

## Context
Holdings were tagged to goals by a fixed list (`ISIN_GOAL_MAP`, name patterns) written in May 2026: SGBs and small caps to marriage, ELSS to health, mid caps to education. It never looked at a goal's date or how
funded it was, ignored FDs and SGBs held outside the broker, and could not tell a tag the owner set from one it had set itself. The owner asked that money already in funds, stocks and deposits count toward the goal it
suits, matched from the market and the goals, continuously, with nobody there when a fund is sold or bought.

## Decision
- `app/planning/matching.py` assigns each holding (and each non-liquid manual asset) to the goal whose asset-class bucket (need today × the mix recommended for the horizon) is emptiest, with eligibility rules
  (single stocks 10+ years, PPF 5+, the emergency fund debt only and filled first) and a stickiness rule (move only when the current goal wants it under half as much as the best).
- Provenance: `holdings.goal_tag_source` and `manual_assets.goal_tag_source` (`manual` | `auto` | NULL). Setting a tag in the UI writes `manual`; clearing it releases the holding. NULL with a tag equal to the old fixed rule counts as automatic.
  The source is carried to new snapshot rows (`inherit_tags`, the roll-forward) and exposed by the holdings API and the `latest_holdings_deduped` view.
- Triggers: after each sync for untagged holdings (`auto_tag_holdings`), daily sweep for all automatic ones, `POST /api/accounts/holdings/auto-tag` (`dry_run` lists the moves), and when a tag is cleared.
- The old fixed rules stay only to recognise pre-matcher tags and as a fallback when the planner is unavailable.

## Consequences
First run (21 Oct 2026, backup `pre_goal_matching_20261021`): ten moves: SGBs and a gold ETF left Child Marriage (a 22-year goal holding gold far above its 5% bucket) for goals with room; six untagged FDs and one SGB were placed
(two FDs closed the emergency gap). Corpus per goal moves; the total does not. Tests: `tests/test_planning_matching.py`. Limits in docs/PLANNING.md §6.
