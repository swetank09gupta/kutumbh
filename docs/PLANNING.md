# Planning: how goal numbers are derived

There is **one calculator** for goals (`app.engine.goals.project_all_goals`). The goal cards, the projections table, the scenarios window, the stored
SIP, the recommendations and Grafana all read it. It runs a simulation (`app/planning/`) and fills the *existing* projection fields: the target,
the SIP needed, the projected corpus, the chance, the return, the inflation. There is no separate planning page.

It gives **ranges and probabilities, never a promise**: 15–20 years of history is one regime and cannot see a change of regime. Not investment advice.

## 1. What is derived and what is yours

| Goal | Target | Basis |
|---|---|---|
| Family retirement (one goal) | **derived**: the corpus that lasts with 80% chance | spending, inflation, ages, spouse income |
| Child higher education | **derived**: abroad undergraduate + postgraduate, paid by the child's age | cost ladder, education inflation + rupee |
| Emergency fund | **derived**: months of spending (9 with one earner and a loan, else 6) | spending |
| Health & senior care | **derived**: a ₹10 L individual policy plus expected bills for each of you from your own retirement (the employer's cover ends) to your own life-table horizon | public premium and NSS/LASI cost figures |
| Child marriage | **your figure**, grown at a derived inflation (the child's age then is after postgraduate study) | no data to derive from |

Tick **"use my figures"** on a goal to plan on the target (and return) you typed; the derived value is still shown beside it. Your typed target, date,
retirement age, risk appetite and tax regime are never overwritten.

## 2. Inputs, all from the app

- **Spending** (last 12 complete months, `v_cashflow`). Retirement living cost = spending minus loan EMI, term-insurance premium, child costs, family support and medical bills
  (they end, or other goals fund them; health is the Health goal's). School fees stay in today's cash flow until class 12; college starts the education goal's payments.
- **Own spending trend**: core spending in the last three 12-month windows. If it grew faster than prices the excess (at most 3 points) is carried forward, otherwise nothing; a fall is never counted on.
  Only about two clean years of statements exist, so today it reads *falling* (₹22.7 L → ₹18.0 L → ₹18.6 L) and adds nothing. Family money (parents) is a transfer, never income or spending: names in `config/accounts.yml` → `family_names`.
- **People** (`owners`, `config/accounts.yml`): dates of birth, retirement ages (you 50, your wife 60), risk appetite, tax regime; the child's date of birth.
- **Holdings tagged to a goal**, by owner (mix of equity / gold / debt / PPF, and whose money it is); each person's **SIP**; the **loan**.
- **Earnings**: each person's salary credits (a working spouse offsets spending until their own retirement).

## 3. Derived assumptions

Public parameters (each with a source and the day it was read) are in `app/planning/params.yml`; live inputs come from the app's market signals,
CPI module and FRED. Scenarios shift the market together: *cautious* (equity −2, gold −1, debt −0.5 points; inflation +1), *hopeful* (+1.5, +0.5, +0.5; −0.5).

| Quantity | Derived as |
|---|---|
| General inflation | mean of the 10-year CPI average (World Bank), the latest year-on-year print and the RBI's own projection ([SCC Online](https://www.scconline.com/blog/post/2026/10/09/rbi-hikes-repo-rate-to-5-50-percent-october-2026/)) |
| Medical inflation | general + 6 points (Aon 2026 medical trend 11.5%; Milliman 12–13% vs general 4.2%, [Milliman](https://www.milliman.com/en/insight/measuring-medical-inflation-in-india)) |
| Education, India | general + 4 points (10–12% in [RetireWise](https://www.retirewise.in/?p=5782); an 8% modelling assumption elsewhere) |
| Education, abroad | US tuition inflation 3.5% (College Board 2025-26: +2.9 to +4.0%, [report](https://research.collegeboard.org/media/pdf/Trends-in-College-Pricing-and-Student-Aid-2025-final_0.pdf)) + the rupee's 10-year fall against the dollar (FRED USD/INR, live) |
| Wedding | general + 2 points (judgement: no published index) |
| Retirement spending | general inflation plus the own-spending drift above (0 today); medical is in the Health goal, not here |
| Long-run fade | a category's premium over general inflation (medical, education, wedding) applies in full for 10 years, then fades to a quarter of it at year 40 (judgement: medical at 10.9% for 46 years would be a 130-fold rise, more than any income grows) |
| Debt return | 10-year G-sec yield (live) less 0.3 fund cost |
| Equity return | debt + the **net equity premium** the real history paid over gilts (about 20 years, from AMFI NAVs), scaled by valuation ((long-run P/E ÷ today's P/E)^0.5, between 0.7 and 1.25), at least 1.5 points |
| Gold return | general inflation + half of gold's real return since 2011, capped at 2 points (the sample holds the 2025-26 spike) |
| PPF | the declared rate (`small_savings.ppf_rate_on`) |
| Salary growth (yearly SIP rise) | 9% (Aon 2026 India projection, [Aon](https://www.aon.com/apac/in-the-press/asia-newsroom/2025/salaries-in-india-projected-to-increase-by-nine-percent-in-2026-aon-study)); a rise typed on the goal wins |
| Spouse income counted | 80% of the last 12 months' salary (job and career risk), growing with salary |
| Retirement horizon | until the chance that **either of you is still alive** falls to 15%, from the SRS 2016-20 urban life table (female column for both, so no sex is assumed; Gompertz tail beyond 85). About 94 for the younger of you; once it is more likely than not that one has died (about 71), household spending is 75% (OECD scale gives 67%) |
| Senior health cost | ₹45,000 a year per person for a ₹10 L individual policy (₹35-55 k, 60-70 years, metro) + 8.1% (NSS 2025, 60+) × ₹49,700 (LASI private out-of-pocket) of bills ≈ ₹49 k, at medical inflation |
| Retirement mix | 40% equity, 5% gold, 55% debt; each goal glides to its horizon's mix (equity 75% at 10+ years, 60/45/30/10% nearer; emergency 0%; risk appetite ±10) |

**Tax is per person** (`app/planning/taxes.py`), not a flat drag. Debt income is taxed at the holder's **own** marginal rate: the band of the highest annualised taxable income
of the last three years (their regime, from the tax view; salary is as credited, so it is a floor), plus surcharge and 4% cess. Surcharge above ₹50 L / ₹1 cr / ₹2 cr is 10 / 15 / 25%
([BankBazaar](https://www.bankbazaar.com/tax/surcharge-on-income-tax.html); the new regime is capped at 25%; one page conflicts on the lowest band). Equity pays LTCG (12.5% + cess, the rule
registry) once at the end, so the yearly cost falls the longer it compounds; gold pays half of that (the SGB part is exempt at maturity); PPF pays none. In retirement debt is taxed at the new-regime
rate on a retiree's income and equity on the gain share of each withdrawal. A goal funded by both of you is weighted by whose money is in it.

## 4. The simulation (`app/planning/simulate.py`)

Real month-end NAVs from AMFI (UTI Nifty 50 Index, SBI Gold, UTI Gilt; regular plans, net of costs) are resampled in 6-month blocks, so crashes, recoveries and the co-movement
of the three assets are real. Each path's average is re-centred on the derived expectations above and equity volatility is scaled to the 20-year sample (it includes 2008).
Inflation is drawn once a year per path (categories move together). 3,000 paths, fixed seed.

A goal is contributions (a SIP shape, each person's SIP ending at their own retirement) and outflows (a cost on a date, tuition at the child's age 18–21 and 24–25, or a lifetime of spending net of a
spouse's income). Discounting at each path's realised returns, the plan survives on a path iff `corpus + scale × PV(contributions) ≥ PV(outflows)` at every month, so the smallest workable scale per
path is closed-form and the **SIP for p-confidence is its p-quantile**: no search, the same answer every run. The card shows the SIP for an 80% chance, and the target is what gives 80%.

## 5. Limits

- History is one regime (falling inflation, a long bull phase); probabilities are conditional on it repeating in distribution. Returns are re-centred on cautious means for that reason.
- Inflation is independent of returns in the draws (high inflation often hurts both): mildly optimistic.
- The primary earner's taxable income is a floor (salary credited is after TDS); surcharge probably applies and is not counted until the credited figure shows it.
- Education costs are today's published ranges (undergraduate abroad ₹60 L, postgraduate abroad ₹1.1 cr, India ₹20 L + ₹27 L); `params.yml` holds them with their ranges.
- Family history (parents' and grandparents' ages at death, heart disease, diabetes, cancer, hypertension) is not in any data: the life table is the population's, for urban women, with no improvement in medicine. A long-lived family should lift it.
- Spending in retirement is flat in real terms (no extra for travel) apart from the one step when a spouse is probably gone. Mortality of the two is treated as independent.
- Health: one flat cost per person-year from retirement (age bands within 50-85 are not modelled); a catastrophic uninsured illness is not in the number: keep part of the emergency fund for it.
- Only the last ~2 years of spending are clean (statement coverage before 2024 is partial), so a five-year trend cannot be read from the data yet; it will be as years accumulate.

## 6. Which holding serves which goal (`app/planning/matching.py`)

Money already held counts toward the goal it is tagged to, and **the matcher chooses the tag**, from the goals' own numbers instead of a fixed fund-to-goal list:
- Each goal has a need today (the planner's required corpus discounted to today; for the emergency fund its target) and a **mix it should hold at its horizon** (equity share by years left, §3; the emergency fund all debt).
  A goal's bucket for each asset class is need × mix; what it already holds fills it.
- A holding goes to the goal whose bucket for what it is made of (equity / gold / debt; PPF as debt; hybrids 60/40) is emptiest and can take it. A single company's shares only suit goals 10+ years away, PPF 5+, equity never the emergency fund.
  Untagged debt holdings (FDs) first close the emergency fund's gap.
- **A holding is moved only when its goal no longer wants it half as much as the best goal does**, so labels do not churn. A second run changes nothing.
- **Tags you set in Holdings are `manual` and never moved**; the matcher's are `auto` (shown as "auto"); clearing a tag hands it back. Tags that predate the matcher count as automatic only if they equal the old fixed rule's answer.
- It runs after every sync (gaps only) and in the daily sweep (every automatic holding): a sale shrinks a goal's holdings, a purchase lands where the gap is, a goal's date moving changes its mix. Liquid FDs stay with the emergency fund and a home is not a corpus.
- Not measured: a fund's own volatility or category from AMFI (the asset class comes from its name/type), so a mid-cap and a large-cap count the same; single-stock risk is the one exception.

## 7. Keeping it current

`params.yml` entries carry `verified_on`; replace a value by adding a newer dated entry. Market inputs refresh with the app (market signals 12 h, CPI 24 h, USD/INR 7 days, AMFI history 30 days).
When the repo rate, CPI or a cost range moves, re-read its source and add the dated entry; the planner picks the latest.
