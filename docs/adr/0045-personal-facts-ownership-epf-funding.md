# ADR-0045: Personal facts the data cannot show: EPF, home ownership, pay, family history, a term plan; and a funding order

**Status:** Accepted
**Date:** 2026-10

## Context
The owner supplied facts no statement carries: his EPF passbook (₹16.3 L; ₹71k a month going in), that his father is a joint holder with 33% of the flat and the loan (so the father's money into his account is his own share of the loan, not a gift),
his current pay (₹72 L CTC from 3 Aug 2026), family longevity (grandparents 85 and 87+, parents 64 and 61), a return-of-premium term plan (premium "for ten years", "about ₹61 L back at 72"), and a wish to fund goals by their dates.
Before this, retirement looked unreachable (15% chance) because it counted only SIPs and the market holdings, and net worth counted the whole flat and loan.

## Decision
- **EPF** (`app/engine/epf.py`): the balance and monthly contribution in `config/accounts.yml` become a Manual Asset (type `epf`, tag retirement), valued by rolling forward at the declared rate (contributions at month ends, interest monthly on the running balance, credited each 31 March);
  a warning after 13 months. It counts in net worth ("other"), the retirement goal's corpus (beside PPF) and the planner: its contributions are fixed inflows (at 80%), the fixed-rate bucket earns the value-weighted PPF and EPF rates, EPF's after-tax rate leaves out the taxable
  interest on employee contributions above ₹2.5 L a year. Not fetched: the portal needs a captcha login.
- **Home ownership**: `home_loan_share_pct` (33 / 34) now also decides net worth: the flat and the loan count for each co-owner at their share (a parent's 33% is nobody's here). Family net worth fell from ₹4.06 cr to ₹3.25 cr; revert by setting a share to 100.
- **Pay**: configured CTC less the employer's PF share and the standard deduction sets the tax slab where it exceeds the after-TDS credit (34.3% for the owner). **Capacity** from cash flow counts a new employer from its first full month.
- **Family history** shifts a person's survival curve (heritability 0.25 of how far ancestors outlived 78.3, cap 3 years); **a return-of-premium plan** is a fixed inflow at its age; its premium end is dated in the capacity.
- **Funding order** (`goal_plan.apply_funding`): emergency first, then by date (education, health, retirement, marriage) or `goal_priority`; a working saving is kept, the remainder tops up in order; shown in the existing goal notes.
- **Silver** is a factor of its own (rupee silver from COMEX and USD/INR); **fund portfolios** from Groww and AMFI's cap list replace the regression as the first source (ADR-0044 addendum).

## Consequences
Retirement: chance 79% at today's saving, an 80% chance from retiring at 51 (was 15%, and age 70): EPF contributions of ₹8.5 L a year to 2042 and the return of premium carry most of it. The estimate depends on the salary and EPF assumptions (9% growth at 80%, 7% after tax); the wife's EPF/NPS and the
exempted employers' PF trusts are unknown and not counted. Tests: `tests/test_ownership_epf_db.py`, `tests/test_planning_plan.py::PersonalFacts`, `tests/test_planning_funds.py`.

## Correction (2026-10-21, from the policy document)
HDFC Life Click 2 Protect Super, policy 28077956: premium for **12 years** (last due 16 Sep 2036, ₹41,369 a month with riders), 40-year term, **return of premium ₹53,55,900 at maturity (16 Oct 2064, age 72)**, death cover ₹13.76 cr, riders: cancer ₹30 L, accidental
disability income ₹10 L, waiver of premium on critical illness. The plan used ₹61 L and ten years from memory; it now reads the document's figures (`final_premium_date`, `maturity_date`, `return_amount` in the config). The retired-employer PF is inside the shared EPF balance.

## Addendum (2026-10-24): the wife's EPF and NPS
From her PF statement for FY 2023-24 (opening April 2023: member ₹16,45,166 + company ₹7,70,101; employee 12% of basic+DA, company the same, VPF on top in some months) and the April 2025 balance she shared (member ₹23,37,216 + company ₹11,80,778 = ₹35,17,994), rolling the 2023 opening forward at the declared 8.25%
with the statement's contributions lands at ₹35.3 L, within 0.4% of what she shared: the roll-forward model holds. `config/accounts.yml` has her entry (anchor 31 Mar 2025; this full year's 93,137 member + 93,137 company + 59,170 VPF = ₹7,761 / ₹7,761 / ₹4,931 a month; contributions assumed flat: her 3% yearly rise and DA changes are not
modelled in the roll-forward). `voluntary_monthly` (VPF) grows the balance, counts toward the ₹2.5 L taxable limit and, being locked in until retirement like the rest, counts in the plan's contributions. Her **NPS** (new `nps` block: ₹8,413 a month from April 2026, balance 0 on 31 Mar 2026) is valued by what has gone in (no NAV is known, no return
assumed), counts in net worth ("other"), the retirement corpus (tag retirement; weights 50/50 equity/debt) and as a monthly inflow in the plan. A corrected contribution on the same anchor date now updates the stored asset. The warning "EPF balance over a year old" stays until a newer balance is shared (once a year is enough).
