# ADR-0042: Goal numbers are derived from spending, real history and simulation; yours are shown beside them

**Status:** Accepted; the separate Plan card and `/api/plan` it describes were replaced by feeding the one goal calculator (ADR-0043)
**Date:** 2026-10

## Context
The goals were typed months ago ("very basic"): ₹5 cr retirement, ₹5 cr child education, ₹1 cr marriage. The projection used a single rate per goal,
and a blended-return rule with a 4% floor that fell back to the goal's preset 12% — a *higher* assumed return, which lowers the SIP needed, so weak
data made the plan more optimistic (a 0.03-point change moved "Your Retirement" SIP from ₹1.49 lakh to ₹93,000 overnight). Nothing said how likely a goal
was, what it needed at a given confidence, or whether the target itself was sensible.

## Decision
- **Method:** `app/planning/` — real monthly returns of equity, gold and debt (AMFI NAVs via mfapi.in, net of fund costs, cached 30 days), resampled in 6-month blocks,
  **re-centred** on cautious forward assumptions (equity 10.5%, gold 7%, debt 7%, PPF 7.1%, less a tax drag that is halved in retirement), inflation drawn per year.
  5,000 paths, fixed seed. Assumptions, sources and limits: `docs/PLANNING.md`.
- **Outputs:** chance of reaching the inflated target; SIP for 80/90% confidence (closed form: wealth is linear in the contribution, so no search and no noise);
  retirement as "does it last" (the corpus needed is the present value of spending at each path's realised returns), by retirement age and horizon, under three markets.
- **Derived, not typed:** retirement living cost = last 12 months' spending minus loan EMI, term premium, child costs and family support.
- **Yours stay yours:** targets, dates, retirement age and risk appetite are never overwritten; the derived value and re-based options are shown beside them.
- **Capacity** is recurring income minus spending (unlabelled credits excluded); prepay-vs-invest compares the loan rate with simulated after-tax outcomes.
- The existing projection (`project_all_goals`) and its 4% rule are **unchanged** here; the 4% rule is recorded in the roadmap inbox for a decision.

## Consequences
`GET /api/plan/` (cached 10 min), the Goals → Plan card. Tests: `tests/test_planning_engine.py` (closed-form proofs with volatility off, monotonicity, seed
repeatability, the average equals the assumption), `tests/test_planning_plan.py`. First run (21 Oct 2026): every goal at today's plan has a near-zero chance;
the retirement need depends mainly on the retirement age; education at ₹5 cr is out of line with public cost ladders.
