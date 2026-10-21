# ADR-0043: One goal calculator, derived targets, per-category inflation and per-person tax

**Status:** Accepted
**Date:** 2026-10

## Context
ADR-0042 added the planning engine but as a separate Plan card and `/api/plan`: a second calculator beside `project_all_goals`, against the rule "one definition per number" and the
owner's wish to keep the UI simple and improve existing logic rather than add cards. The owner also said the goal figures were typed months ago ("very basic"), that targets should follow current
spending, inflation (health, education), regime and tax **per person**, that retirement is **one household goal** (he 50, his wife 60), that education is planned **abroad** with India as the fallback, and
that inflation should come from the market and be applied to each goal. Decision 29 ("one app-wide inflation, never per-goal") contradicts the last point.

## Decision
- **The planner feeds the existing calculator** (`project_all_goals` → `goal_plan.overlay`), field for field. The Plan card and `/api/plan` are removed. The goal card, the projections table, the scenarios
  window (cautious / hopeful / no yearly rise instead of the glide knobs), the stored SIP, the recommendations (emergency months, equity share for the horizons, the retirement need) and the loan card
  (prepay or invest) read it. A goal the planner cannot plan (no market history, custom type) keeps the deterministic projection.
- **Targets are derived** for retirement, education and emergency, from spending, ages, the child's date of birth and the cost ladders; marriage and health stay the owner's figure, grown at a derived inflation.
  `use_manual_return` on a goal now means "use my figures" (target and return). The derived value is shown beside the typed one; the chance and the SIP are for an 80% confidence.
- **Revises decision 29:** inflation is derived per category from live data (10-year CPI average, latest print, RBI projection) plus sourced spreads (medical, education, wedding) or, for education abroad,
  US tuition inflation plus the rupee's fall (FRED). It still uses the long-run view (never the latest print alone), which was the reason for 29. The app-wide setting (`/api/goals/inflation`) now only feeds the
  deterministic fallback.
- **Per-person tax** (`app/planning/taxes.py`): each holder's regime and the highest annualised taxable income of the last three years set the marginal rate on debt income; equity and gold pay capital-gains tax by horizon.
- **One household retirement goal**, starting when the first earner stops, with a working spouse's salary offsetting spending until their own retirement and money lasting until the younger spouse is 95. The goal tags
  of a family retirement goal are both retirement tags; the two former goals were merged (backup `pre_goal_merge_20261021`).
- **Family money is a transfer**: names in `config/accounts.yml` → `family_names` (parents' cheques and RTGS were counted as income-like "other" credits: ₹32 lakh in a year).
- Expected returns are derived: debt = the 10-year yield less cost; equity = debt + the net premium the history paid, scaled by valuation; gold = inflation + part of its real return; PPF = declared.

## Consequences
Goal figures move by design (targets, SIPs, chances). Tests: `tests/test_planning_engine.py` (closed-form proofs, monotonicity, repeatability), `tests/test_planning_plan.py` (expectations, tax, goal outflows,
spouse offset, prepay), `tests/test_txn_layer.py` (family payees). Costs and spreads live in `app/planning/params.yml` with sources and dates; the method is `docs/PLANNING.md`.
