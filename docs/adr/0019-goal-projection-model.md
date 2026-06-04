# ADR-0019: Goal projection — blended return, SIP step-up, glide path

**Status:** Accepted
**Date:** 2026-06

## Context

Goal projections previously used a single `expected_return_rate` (default 12%) applied flat to the entire goal horizon, with a flat current-SIP assumption. Three things were off:

1. A fund that has actually returned 16% historically should inform its goal's projection — but the user has to manually edit the goal to know that, and is likely to over- or under-shoot.
2. Real SIPs grow each year as income grows. Projecting a flat ₹40k SIP for 25 years under-counts the corpus by ~40–60% versus a realistic 8–10% step-up.
3. Aggressive equity returns (12–14%) all the way to the goal date assume you'll stay 100% equity-loaded on the day you withdraw. In practice retirees / parents shift to debt 3–5 years out; the projection should match.

Industry convention for target-date funds bakes all three in.

## Options

- **(A)** Add per-goal scalar fields for everything (return, step-up%, glide start, glide end) and let the user fill them. — Trustworthy but boring; defaults still have to be right.
- **(B)** Derive everything automatically from holdings + transaction history. — Magical when it works, opaque when it doesn't.
- **(C)** Auto-derive by default, manual override when needed, surface what was used. — Combines the two; the user sees a "blended XIRR 14.2% (auto)" badge and can flip to manual.

## Decision

Option (C). Specifically:

### Effective return
- `Goal.use_manual_return` (bool, default `False`) flips between auto and manual sourcing.
- Auto: value-weighted blend of `XIRR` (or fallback `CAGR`) of holdings + manual assets whose `goal_tag` matches the goal's `goal_type`.
- Each component is capped at a long-run category benchmark before blending (equity 13.5%, MF 13%, FD 7%, gold 9% — see `goal_returns.CATEGORY_CAPS_PCT`). This prevents a fund that did 28% in a 3-yr bull run from extrapolating at 28% for 30 years.
- If the blended figure < 4% (e.g. fresh holdings sitting at a paper loss), fall back to a conservative 12% default. Explicit floor — better than projecting permanent loss.
- The projection response surfaces `effective_return_pct`, `return_source ∈ {auto, manual, default}`, and the component breakdown for tooltip rendering.

### SIP step-up
- `Goal.sip_stepup_pct` (numeric, nullable). Null = auto-detect.
- Auto-detect compares the family's investing rate in months [-11..0] vs [-23..-12]; the ratio gives an annualized growth %, clamped to 0–12% (a 60% one-off jump from starting a new SIP shouldn't extrapolate forever).
- Math: step-up applied at each completed 12-month boundary inside the FV-of-annuity loop. SIP-year-n = SIP-year-0 × (1+s)^n.
- The SIP-needed solver (binary search on initial monthly SIP) operates under the same step-up + schedule.

### Glide path
- `Goal.glide_enabled` (bool, default `True`), `glide_start_years_before` (int, default 3), `glide_end_return_pct` (numeric, default 8).
- The return is delivered to the FV math as a per-month schedule (`_monthly_return_schedule`): flat at the start-rate until `total_months - glide_months`, then linear taper to `glide_end_return_pct` over the glide window.
- Same schedule is consumed by both lump-sum compounding and step-up SIP FV, so they're internally consistent.

### UI surface
- Goal modal exposes all four knobs.
- Projections table shows the effective return %, source badge (`auto`/`manual`/`default`), and chips for step-up and glide when active.
- `/api/goals/scenarios/{id}` returns side-by-side projections for `base / no_stepup / no_glide / flat` so the user can see how much each knob is moving the answer.

## Consequences

- ✅ Projections track the user's actual portfolio behaviour as data matures.
- ✅ Defaults are conservative enough that "I didn't tag anything yet" still gives a sensible projection.
- ✅ A scenario panel makes the knobs concrete instead of theoretical.
- ❌ More moving parts. A user who doesn't understand "auto vs manual" might be confused by the badge. Mitigated by tooltips + the scenarios endpoint.
- ❌ Category caps are hard-coded India long-run benchmarks; no global story. Acceptable for a household tool with a known geography.
- ❌ Step-up auto-detect is noisy for owners with <12 months of history. Returns `None` in that case, projection treats as 0%.

## Notes

- Tax on gains is **not** modeled — projections are pre-tax. The user's target should be set pre-tax to match.
- Glide path defaults (3y / 8%) are tighter than typical target-date funds (5y / 5%). Picked to match Indian retiree behaviour, which de-risks later and tolerates more residual equity.
