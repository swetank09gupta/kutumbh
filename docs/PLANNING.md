# Planning: how goal numbers are derived

Written 21 Oct 2026, before the code (`app/planning/`). It replaces "a target I typed months ago" with numbers derived from what you
actually spend, what history says each asset did, and what rising inflation does to costs. It gives **ranges and probabilities, never a
promise**: history of 15–20 years includes one strong decade and cannot see a regime change. Nothing here is investment advice.

## 1. Method

1. **Today's facts, from the app** (never retyped): spending by category over the last 12 months (`v_cashflow`), income, net worth,
   liquid assets, the home loan (rate, EMI, balance), each goal's tagged holdings, current SIP, and ages (`owners`).
2. **Real history, pulled live and cached 30 days** (`app/planning/history.py`): month-end NAVs from AMFI via mfapi.in for
   equity = UTI Nifty 50 Index Fund Regular (2006→), gold = SBI Gold Fund Regular (2011→), debt = UTI Gilt Fund Regular (2006→);
   India CPI from the World Bank (already in the app). These are **net of fund costs**, i.e. what an investor received.
3. **Simulation** (`app/planning/simulate.py`): 5,000 paths, joint monthly returns of the three assets resampled in 6-month blocks
   from the real history (keeps crashes, recoveries and the equity/gold/debt co-movement), **re-centred** on the forward-looking
   means below and with equity volatility scaled to the 20-year sample. Inflation is drawn per year around the means below.
   Fixed seed: the same inputs always give the same answer.
4. **Outputs per goal:** probability of reaching the (inflated) target at the current plan; the 10th/50th/90th-percentile outcome;
   the monthly SIP that gives 50/80/90% confidence; the recommended equity share for the horizon and the drift from it.
5. **Retirement is not a fixed corpus.** It is "does the money last": the corpus needed at retirement is the present value, at the
   returns each path actually delivers, of the spending that follows. Reported at 80/90/95% confidence, for several retirement ages.

## 2. Assumptions (base case) and where they come from

| Assumption | Base | Basis (source, date) |
|---|---|---|
| Equity return, before tax | **10.5%** | Net-of-cost index fund: 10.05% a year over 20 years (2006–26) and 11.5% over 15 (computed from AMFI NAVs, 21 Oct 2026). Index TRI (gross) 13.4% over 10 yrs, 12.4% over 20 (Bajaj AMC, May 2026, [source](https://www.bajajamc.com/knowledge-centre/nifty-50-historical-returns)); rolling 15-year average ~14%, range 9–19% ([FundsIndia, Sep 2026](https://fundsindia.com/blog/wp-content/uploads/2026/09/202609-FundsIndia-Wealth-Conversations.pdf)). Set between the net samples because valuation is fair, not cheap (Nifty 50 P/E ~20–24 vs a 20–22 long-run range, [Wright Research](https://www.wrightresearch.in/blog/nifty-pe-ratio-2026/)) and the 10-year yield (7.1–7.4%, [Trading Economics](https://tradingeconomics.com/india/government-bond-yield/news/591417)) leaves a thin equity premium. |
| Gold return | **7.0%** | The 15-year sample is 10.6% but includes the 2025–26 spike (3-year CAGR 37%, app's metal history); long-run is taken as inflation plus about 1.5 points. |
| Debt (gilt/bond funds, FDs) | **7.0%** | The 10-year G-sec yield today, 7.1–7.4%; the gilt fund sample is 7.8% (AMFI NAVs). |
| PPF | **7.1%**, no volatility | Declared rate, unchanged for Oct–Dec 2026 ([Business Today](https://www.businesstoday.in/personal-finance/story/small-savings-scheme-interest-rates-unchanged-for-q3-fy27-check-ppf-scss-ssy-rates-558892-2026-09-30)). |
| Tax drag on return | saving years: equity −0.6, gold −0.5, debt −1.8, PPF 0 (points a year); retired: about half (−0.3, −0.3, −0.9, 0) | Equity LTCG 12.5% above ₹1.25 lakh at exit; debt funds/FDs taxed at slab (old regime, 30% + cess for you) during saving; PPF is tax-free. Rates from the app's rule registry. Retirement income is lower (a lower slab) and the yearly LTCG exemption covers more, so the drag is about half. Found necessary: the full drag made the retirement need unrealistically high. |
| Volatility, correlation | from history | Monthly 2011–26; equity volatility scaled ×1.26 to match the 20-year equity sample (20.3% vs 16.1%) because the shorter joint window misses 2008 (equity fell 59.7% peak to trough). |
| General inflation | **5.5%**, σ 1.8 | 10-year CPI average 4.74% (World Bank); CPI 4.82% in Aug 2026, RBI FY27 projection 5.2%, Nomura sees ~6.3% in Q4 ([SCC Online](https://www.scconline.com/blog/post/2026/10/09/rbi-hikes-repo-rate-to-5-50-percent-october-2026/), [Outlook Money](https://www.outlookmoney.com/banking/nomura-sees-rbi-repo-rate-rising-to-575-per-cent-with-two-25-bps-hikes)). |
| Medical inflation | **11.5%** | Aon 2026 medical trend 11.5% ([Aon](https://www.aon.com/apac/in-the-press/asia-newsroom/2025/india-outpaces-global-average-with-2026-medical-trend-at-11-5-percent-aon-reports)); Milliman 12% (2024), 13% (2025) vs general 4.2% ([Milliman](https://www.milliman.com/en/insight/measuring-medical-inflation-in-india)). |
| Education inflation | **9%** (range 8–12) | Sources disagree: 10–12% ([RetireWise](https://www.retirewise.in/?p=5782)), an 8% modelling assumption ([Basunivesh](https://www.basunivesh.com/cost-of-education-in-india-2025-2040-fees-living-projections/)). No official series found. |
| Retirement spending basket | 85% general + 15% medical → about 6.4% | Retirees spend more on health; a basket weight, not a forecast. |
| Withdrawal rule of thumb | **not assumed** | Practitioners quote 3–3.5% for India ([Basunivesh](https://www.basunivesh.com/safe-withdrawal-rate-india/)); none is backtested on Indian data. The simulation derives the supportable rate from the data instead. |
| Life horizon | **to age 95** (85 shown too) | Longevity risk is the larger error; family history (below) should adjust it. |

A goal's own inflation or return typed in the app overrides these for that goal and is shown next to the data-based value.

## 3. What is *derived* and what is *yours*

- **Derived:** spending, retirement spending (core living cost: total spending minus loan EMI, term-insurance premium, child costs and
  family support, which end or are funded by other goals), probabilities, SIP needed, allocation drift.
- **Yours (we never overwrite):** the target date, the retirement age, the target amount, risk appetite. The app shows the *derived*
  value beside yours so the difference is visible.
- **Needed from you, not in any data:** family history (parents' longevity and health, hereditary conditions), the child's age,
  other retirement assets (EPF, NPS, property income), whether education is in India or abroad.

## 4. Limits (read before trusting a number)

- 15–20 years of history is one regime: falling inflation, a long bull phase, a rising rupee-gold. Probabilities are *conditional on
  that history repeating in distribution*. The model re-centres returns on cautious means to avoid projecting the past's luck.
- Tax is a flat drag, not a per-person computation. Sequence of withdrawals and tax in retirement are simplified.
- Inflation is independent of returns in the draws (in reality high inflation often hurts bonds and valuations): a mild optimism.
- The loan, salary growth, and job risk are inputs you control, not predicted.
- Rates are moving (repo raised to 5.50% on 7 Oct 2026); re-run when the cycle turns. Assumptions carry a date and are re-checked
  monthly with the history refresh.

## 5. What the screen shows (Goals → Plan)

Per goal: target, what you save now, the **chance** the plan reaches the inflated target, and the monthly SIP for an 80% (and 90%) chance, with a
step-up you choose (default 8% a year, a stated assumption). Each goal expands to **re-based target options** from public cost ladders
(RetireWise 2026: India IIT/IIM/private ≈ ₹28 L, abroad undergraduate ≈ ₹60 L, abroad MBA ≈ ₹1.1 cr), shown beside your own target; nothing is applied
automatically. **Household retirement** pools both retirement goals and shows, for retiring at 50/55/60 and money lasting to 85/95, the chance now, the corpus needed
(today's money) at 80/90/95% and the SIP for 90%, under a cautious, base and hopeful market. **Capacity** is recurring income minus spending
(unlabelled credits are not counted) and what it becomes when the loan ends. **Loan**: payoff time at extra prepayments, and how often a portfolio
beats the loan rate after tax on the real history (prepaying earns the rate for certain).

## 6. Phases

Status is in ROADMAP.md. Built: engine, per-goal odds and SIP, retirement by age, capacity and loan, prepay-vs-invest, the Plan card.


