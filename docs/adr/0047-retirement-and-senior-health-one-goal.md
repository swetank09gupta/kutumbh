# ADR-0047: Retirement and senior health are one goal

**Status:** Accepted
**Date:** 2026-10-24

## Context
Retirement and senior health started on the same date for the same people, but had separate corpora and separate funding. Retirement held a large surplus (98% chance) while Health looked 11% funded and asked for ₹34.8k a month, which also took suggested capacity from education and marriage.
Run as one plan (retirement outflows plus the senior-health stream), the same holdings gave 89% with no saving and 92% with the ₹10k already invested. The split, not the money, made health look short. The owner's view: no SIP funds this; EPF, VPF and NPS (about 12% of salary each for both of them, rising with salary) cover it.

## Decision
- **One goal, "Retirement & Senior Health"** (type `retirement`). `plan_retirement` adds the senior-health stream (`health_schedule`: each person from their own retirement to their own life-table horizon, a ₹10 L individual policy's premium plus the bills hospital stays imply, at medical inflation fading over time) to the living-cost outflows. Living cost still excludes medical bills.
- The Health goal is deleted (data migrated: 79 holding rows and 1 manual asset retagged `health` → `retirement`; the rest of its logic stays available for a custom `health` goal). The legacy fixed-rule tagger no longer returns `health`. The config seeds one goal; `cancer_cover` on the term plan names what covers a catastrophic illness (his, to 2064).
- Contributions (EPF, VPF, NPS) already grow with salary growth inside the plan, so the contribution stream rises as pay does.
- The **₹10 L policy** is an assumption (params.yml `health`, each figure with its source), not owner data: no health policy is recorded; the employer's cover ends at retirement.

## Consequences
Retirement & Senior Health: corpus ₹98.8 L (the former health holdings joined), 92% chance, no SIP needed. Suggested capacity now goes to education and marriage. A ring-fenced medical reserve is the emergency fund's job; a cancer diagnosis is his term plan's rider; heart or kidney illness and anything for her are not covered by any plan here.
Revises ADR-0043 (health as its own derived goal). Tests: `tests/test_planning_plan.py::test_senior_health_is_part_of_the_retirement_plan_and_raises_what_it_needs`.
