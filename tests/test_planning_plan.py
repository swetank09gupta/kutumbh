"""
The data side of planning: holding classification, spending split, derived expectations, per-person tax, and how goals become outflows.
Synthetic history, no network, no database.
"""
import unittest
from unittest import mock
from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd

from app.planning import allocation, expectations, goal_plan, macro, need, simulate, taxes
from app.planning.assumptions import Assumptions

IDX = pd.date_range("2006-05-31", periods=245, freq="ME")


def history(eq=0.0085, vol=0.045, seed=1):
    rng = np.random.default_rng(seed)
    n = len(IDX)
    equity = eq + vol * rng.standard_normal(n)
    debt = 0.0062 + 0.01 * rng.standard_normal(n)
    joint = pd.DataFrame({"equity": equity, "gold": 0.0075 + 0.04 * rng.standard_normal(n), "debt": debt}, index=IDX).iloc[65:]
    return {"joint": joint, "equity_long": pd.Series(equity, index=IDX), "debt_long": pd.Series(debt, index=IDX)}


MACRO = {"as_of": "2026-10-21", "yield_10y": 7.1, "nifty50_pe": 19.3, "nifty50_pe_avg": 22.0, "usd_inr": 95.0, "cpi_10y_avg": 4.74,
         "cpi_latest_year": 2.4, "cpi_latest_yoy": 4.82, "cpi_latest_yoy_as_of": "2026-08", "rbi_cpi_projection": 5.2, "inr_depreciation_10y_pct": 3.6}


class Classify(unittest.TestCase):
    def test_holdings_land_in_the_right_asset(self):
        c = allocation.classify
        self.assertEqual(c("UTI NNX50INDF D-GROW", "mutual_fund"), {"equity": 1.0})
        self.assertEqual(c("HDFC Liquid Fund", "mutual_fund"), {"debt": 1.0})
        self.assertEqual(c("SBI Gold Fund", "mutual_fund"), {"gold": 1.0})
        self.assertEqual(c("GOI SGB 33076 2031", "bonds"), {"gold": 1.0})
        self.assertEqual(c("7.26% GOI 2033", "bonds"), {"debt": 1.0})
        self.assertEqual(c("Quant Multi Asset Fund", "mutual_fund"), {"equity": 0.6, "debt": 0.4})
        self.assertEqual(c("SBI PPF", None, "ppf"), {"ppf": 1.0})
        self.assertEqual(c("SBI FD ...6925", None, "fd"), {"debt": 1.0})

    def test_the_blend_is_value_weighted(self):
        self.assertEqual(allocation.blend([(300.0, {"equity": 1.0}), (100.0, {"debt": 1.0}), (0.0, {"gold": 1.0})]), {"equity": 0.75, "debt": 0.25})
        self.assertEqual(allocation.blend([]), {})

    def test_long_money_takes_equity_near_money_does_not_and_emergency_never(self):
        r = allocation.recommended_equity
        self.assertGreater(r(15), r(8)); self.assertGreater(r(8), r(4)); self.assertGreater(r(4), r(1))
        self.assertEqual(r(15, goal_type="emergency"), 0.0)
        self.assertAlmostEqual(sum(allocation.split_for(r(15)).values()), 1.0)


class GoalsEquityShare(unittest.TestCase):
    def test_emergency_money_pulls_the_share_down_and_far_goals_push_it_up(self):
        f = allocation.goals_equity_share
        only_far = f([(1_000_000, 15, "retirement")])
        with_emergency = f([(1_000_000, 15, "retirement"), (3_000_000, 0, "emergency")])
        self.assertAlmostEqual(only_far, 0.75)
        self.assertAlmostEqual(with_emergency, 0.75 * 0.25)                                   # a quarter of the money is retirement
        self.assertGreater(f([(1_000_000, 15, "retirement")], "aggressive"), only_far)
        self.assertEqual(f([]), 0.60)                                                         # nothing to weigh: the neutral default


class Spending(unittest.TestCase):
    ROWS = [("emi", "emi_regular", 1_500_000), ("insurance", "term_insurance", 500_000), ("insurance", None, 10_000),
            ("child_expenses", "education_care", 300_000), ("family_support", None, 250_000), ("other", None, 900_000), ("shopping", "online", 300_000)]

    def test_retirement_core_drops_what_ends_or_is_funded_elsewhere(self):
        out = need.split_spending(self.ROWS)
        self.assertEqual(out["total"], 3_760_000)
        self.assertEqual(out["retirement_core"], 3_760_000 - 1_500_000 - 500_000 - 300_000 - 250_000)

    def test_the_window_is_twelve_complete_months(self):
        self.assertEqual(need.month_window(date(2026, 10, 21)), (date(2025, 10, 1), date(2026, 10, 1)))


class Expectations(unittest.TestCase):
    def test_the_derived_numbers_follow_the_inputs(self):
        a, why = expectations.derive(MACRO, history(), "base", ppf=0.071)
        self.assertAlmostEqual(a.inflation_general, (4.74 + 4.82 + 5.2) / 3 / 100, places=6)            # mean of three live readings
        self.assertAlmostEqual(a.debt, (7.1 - 0.3) / 100, places=6)                                        # the 10-year yield less fund cost
        self.assertGreater(a.inflation_medical, a.inflation_general + 0.05)                               # medical runs well ahead
        self.assertAlmostEqual(a.inflation_education_abroad, 0.035 + 0.036, places=6)                     # US tuition + the rupee's fall
        self.assertGreater(a.equity, a.debt)
        self.assertEqual(a.ppf, 0.071)

    def test_a_dearer_market_expects_less_and_a_bond_yield_rise_lifts_debt(self):
        cheap, _ = expectations.derive({**MACRO, "nifty50_pe": 16.0}, history())
        dear, _ = expectations.derive({**MACRO, "nifty50_pe": 28.0}, history())
        self.assertGreater(cheap.equity, dear.equity)
        higher, _ = expectations.derive({**MACRO, "yield_10y": 8.1}, history())
        base, _ = expectations.derive(MACRO, history())
        self.assertAlmostEqual(higher.debt - base.debt, 0.01, places=6)

    def test_scenarios_order_returns_and_inflation(self):
        c, _ = expectations.derive(MACRO, history(), "cautious")
        b, _ = expectations.derive(MACRO, history(), "base")
        h, _ = expectations.derive(MACRO, history(), "hopeful")
        self.assertLess(c.equity, b.equity); self.assertLess(b.equity, h.equity)
        self.assertGreater(c.inflation_general, b.inflation_general); self.assertGreater(b.inflation_general, h.inflation_general)

    def test_the_rupees_fall_is_read_from_the_series(self):
        rates = {f"{2016 + i // 12}-{i % 12 + 1:02d}": 67.0 * (1.035 ** (i / 12)) for i in range(0, 12 * 10 + 1)}
        self.assertAlmostEqual(macro.inr_depreciation_pct(rates), 3.5, places=1)
        self.assertIsNone(macro.inr_depreciation_pct({"2026-01": 90.0}))


class Taxes(unittest.TestCase):
    def test_rates_follow_the_regime_and_income(self):
        top = taxes.marginal_rate(5_500_000, "2026-27", "old")                         # 30% slab, 10% surcharge above ₹50 L, 4% cess
        self.assertAlmostEqual(top, 0.30 * 1.10 * 1.04, places=6)
        self.assertAlmostEqual(taxes.marginal_rate(3_000_000, "2026-27", "old"), 0.30 * 1.04, places=6)
        self.assertAlmostEqual(taxes.marginal_rate(1_320_000, "2026-27", "new"), 0.15 * 1.04, places=6)    # the wife's band
        self.assertEqual(taxes.marginal_rate(1_000_000, "2026-27", "new"), 0.0)                              # inside the rebate
        self.assertEqual(taxes.marginal_rate(5_000_000, "2031-32", "old"), 0.0)                               # a year with no slab table

    def test_a_tax_paid_once_at_the_end_costs_less_a_year_the_longer_it_compounds(self):
        d5, d20 = taxes.terminal_drag(0.10, 5, 0.13), taxes.terminal_drag(0.10, 20, 0.13)
        self.assertGreater(d5, d20)                                                                          # deferral: one hit spread over more years
        self.assertLess(d5, 0.10 * 0.13)                                                                     # never more than taxing each year's gain
        self.assertEqual(taxes.terminal_drag(0.10, 10, 0.0), 0.0)

    def test_debt_is_taxed_at_the_holders_own_slab_and_equity_does_not_depend_on_who_holds_it(self):
        profiles = {"user": {"marginal_rate": 0.343}, "wife": {"marginal_rate": 0.156}}
        self.assertAlmostEqual(taxes.weighted_debt_rate(profiles, {"user": 1.0}), 0.343)
        self.assertAlmostEqual(taxes.weighted_debt_rate(profiles, {"user": 1.0, "wife": 1.0}), (0.343 + 0.156) / 2)
        a = replace(Assumptions(), equity=0.10, debt=0.07)
        s1, r1 = taxes.drags(a, 15, 0.343, 800_000, date(2026, 10, 21), "2026-27")
        s2, r2 = taxes.drags(a, 15, 0.156, 800_000, date(2026, 10, 21), "2026-27")
        self.assertEqual(s1["equity"], s2["equity"])
        self.assertGreater(s1["debt"], s2["debt"])
        self.assertLess(r1["debt"], s1["debt"])                                                              # a retiree pays less on debt


def context(**over):
    owners = {"user": {"name": "U", "dob": date(1992, 3, 9), "retirement_age": 50, "risk": "moderate", "regime": "old"},
              "wife": {"name": "W", "dob": date(1991, 8, 28), "retirement_age": 60, "risk": "conservative", "regime": "new"}}
    a, why = expectations.derive(MACRO, history(), "base", ppf=0.071)
    base = dict(today=date(2026, 10, 21), macro=MACRO, data=history(), base=replace(a, n_paths=600), why=why,
                profiles={"user": {"regime": "old", "marginal_rate": 0.343, "fy": "2026-27"}, "wife": {"regime": "new", "marginal_rate": 0.156, "fy": "2026-27"}},
                spending={"retirement_core": 1_870_000.0, "total": 4_520_000.0}, owners=owners, salary={"user": 5_400_000.0, "wife": 1_060_000.0},
                loan={"outstanding": 1.06e7, "emi": 170000.0, "rate_pct": 7.1}, stepup=0.08)
    base.update(over)
    return goal_plan.Context(**base)


ITEMS = [("user", 1_000_000.0, {"equity": 1.0}), ("wife", 2_000_000.0, {"ppf": 1.0})]


class GoalPlans(unittest.TestCase):
    def test_education_is_paid_by_the_childs_age_not_on_one_date(self):
        sched = goal_plan.education_schedule(date(2022, 4, 19), True)
        self.assertEqual([d.year for d, _a, _c in sched], [2040, 2041, 2042, 2043, 2046, 2047])           # undergraduate at 18-21, postgraduate at 24-25
        self.assertEqual(round(sum(a for _d, a, _c in sched)), 6_000_000 + 11_000_000)
        self.assertTrue(all(c == "education_abroad" for _d, _a, c in sched))
        self.assertTrue(all(c == "education" for _d, _a, c in goal_plan.education_schedule(date(2022, 4, 19), False)))

    def test_emergency_months_depend_on_who_carries_the_household(self):
        one = goal_plan.plan_emergency(context(), 3_000_000, 2_100_000, False)
        self.assertEqual(one["months"], 9)                                                                 # one earner carries it and a loan runs
        self.assertAlmostEqual(one["ideal_target_today"], 9 * 4_520_000 / 12)
        two = goal_plan.plan_emergency(context(salary={"user": 3_000_000.0, "wife": 3_000_000.0}, loan=None), 3_000_000, 2_100_000, False)
        self.assertEqual(two["months"], 6)
        typed = goal_plan.plan_emergency(context(), 3_000_000, 2_100_000, True)
        self.assertEqual((typed["target_basis"], typed["ideal_target_today"] > 2_100_000), ("typed", True))   # the derived value is still shown

    def test_the_household_retirement_starts_with_the_first_earner_and_lasts_for_the_younger_spouse(self):
        r = goal_plan.plan_retirement(None, context(), None, 3_700_000.0, {"user": 11_000.0, "wife": 10_000.0}, ITEMS, 100_000_000.0, False)
        self.assertEqual(r["start_date"], "2042-03-09")                                                    # his age 50
        self.assertEqual(r["first_retiree"], "user")
        self.assertTrue("2083" <= r["horizon_end"][:4] <= "2090")                                          # until the chance either is alive falls to 15%
        self.assertTrue("2055" <= r["step_date"][:4] <= "2070")                                            # more likely than not one of them has died
        self.assertEqual(r["step_factor"], 0.75)
        self.assertEqual(r["retire_dates"]["wife"], "2051-08-28")
        self.assertGreater(r["spouse_offset_today_monthly"]["wife"], 0)
        self.assertEqual(r["target_basis"], "derived")

    def test_survivor_spending_and_the_life_table_lower_the_need_against_a_couple_to_95(self):
        full = goal_plan.plan_retirement(None, context(), None, 3_700_000.0, {"user": 11_000.0}, ITEMS, 1.0, False)
        import app.planning.longevity as lg
        with mock.patch.object(lg, "household_horizon", lambda dobs, today: {"end": date(2087, 3, 9), "step": None, "factor": 1.0, "chance": 0.0}):
            to95 = goal_plan.plan_retirement(None, context(), None, 3_700_000.0, {"user": 11_000.0}, ITEMS, 1.0, False)
        self.assertLess(full["ideal_target_today"], to95["ideal_target_today"])

    def test_when_todays_saving_falls_short_the_earliest_workable_retirement_age_is_named(self):
        r = goal_plan.plan_retirement(None, context(), None, 3_700_000.0, {"user": 11_000.0, "wife": 10_000.0}, ITEMS, 1.0, False)
        self.assertLess(r["chance"], 0.8)
        self.assertIn("age_for_80", r)
        self.assertTrue(r["age_for_80"] is None or 51 <= r["age_for_80"] <= 70)
        rich = goal_plan.plan_retirement(None, context(), None, 3_700_000.0, {"user": 500_000.0}, ITEMS, 1.0, False)
        self.assertNotIn("age_for_80", rich)                                                               # already at 80%: nothing to search

    def test_health_is_planned_from_each_persons_retirement_for_their_own_horizon(self):
        sched = goal_plan.health_schedule(context())
        self.assertEqual(sched[0][0], date(2042, 3, 9))                                                    # his retirement: the employer's cover ends
        self.assertEqual(min(d for d, _a, _c in sched if d.year >= 2051 and d.month == 8), date(2051, 8, 28))   # hers
        self.assertAlmostEqual(sched[0][1], 45_000 + 0.081 * 49_700)                                       # premium + expected bills
        self.assertTrue(all(c == "medical" for _d, _a, c in sched))
        self.assertLess(max(d for d, _a, _c in sched), date(2092, 1, 1))

    def test_health_target_is_the_corpus_needed_when_it_starts_in_todays_prices(self):
        c = context()
        r = goal_plan.plan_staged(None, c, None, 500_000.0, 5_000.0, ITEMS, goal_plan.health_schedule(c), c.base.inflation_medical, "health", 15.4,
                                  5_000_000.0, False, "derived", decumulate=True)
        total = sum(a for _d, a, _c in goal_plan.health_schedule(c))
        self.assertGreater(r["ideal_target_today"], 0)
        self.assertNotAlmostEqual(r["ideal_target_today"], total, delta=total * 0.01)                      # not the plain sum of the payments
        self.assertEqual(r["target_basis"], "derived")

    def test_own_spending_growth_is_carried_forward_only_above_prices_and_capped(self):
        d = need.drift_from_growth
        self.assertEqual(d(None, 4.9, 3.0), 0.0)
        self.assertEqual(d(-9.4, 4.9, 3.0), 0.0)                                                           # a fall is not counted on
        self.assertEqual(d(4.0, 4.9, 3.0), 0.0)
        self.assertAlmostEqual(d(6.9, 4.9, 3.0), 0.02)
        self.assertAlmostEqual(d(20.0, 4.9, 3.0), 0.03)

    def test_a_spouses_income_lowers_what_must_be_saved(self):
        kw = (None, None, 3_700_000.0, {"user": 11_000.0, "wife": 10_000.0}, ITEMS, 100_000_000.0, False)
        with_income = goal_plan.plan_retirement(None, context(), *kw[1:])
        without = goal_plan.plan_retirement(None, context(salary={"user": 5_400_000.0, "wife": 0.0}), *kw[1:])
        self.assertLess(with_income["ideal_target_today"], without["ideal_target_today"])

    def test_more_inflation_raises_the_need_and_a_typed_target_keeps_the_derived_one_visible(self):
        base = goal_plan.plan_retirement(None, context(), None, 3_700_000.0, {"user": 21_000.0}, ITEMS, 100_000_000.0, False)
        hot = context(base=replace(context().base, inflation_general=0.075, inflation_medical=0.13))
        worse = goal_plan.plan_retirement(None, hot, None, 3_700_000.0, {"user": 21_000.0}, ITEMS, 100_000_000.0, False)
        self.assertGreater(worse["ideal_target_today"], base["ideal_target_today"])
        typed = goal_plan.plan_retirement(None, context(), None, 3_700_000.0, {"user": 21_000.0}, ITEMS, 100_000_000.0, True)
        self.assertEqual(typed["target_basis"], "typed"); self.assertAlmostEqual(typed["ideal_target_today"], base["ideal_target_today"])

    def test_a_staged_goal_asks_for_less_saving_when_the_payments_are_later(self):
        c = context()
        now = goal_plan.plan_staged(None, c, None, 500_000.0, 5_000.0, ITEMS, [(date(2040, 4, 19), 6_000_000.0, "education_abroad")],
                                    c.base.inflation_education_abroad, "child_education", 13.5, 0.0, False, "derived")
        later = goal_plan.plan_staged(None, c, None, 500_000.0, 5_000.0, ITEMS, [(date(2046, 4, 19), 6_000_000.0, "education_abroad")],
                                      c.base.inflation_education_abroad, "child_education", 19.5, 0.0, False, "derived")
        self.assertGreater(now["sip_for_80"], later["sip_for_80"])
        self.assertLessEqual(later["chance"] + 1e-9, 1.0)


class PrepayVsInvest(unittest.TestCase):
    def test_a_higher_loan_rate_is_harder_to_beat_and_taxed_debt_loses_to_the_loan(self):
        from app.planning import prepay
        c = context()
        low, high = prepay.compare(c, 6.0, "user", 10), prepay.compare(c, 9.0, "user", 10)
        for mix in low["mixes"]:
            self.assertGreaterEqual(low["mixes"][mix]["beats_loan"], high["mixes"][mix]["beats_loan"])
        self.assertLess(high["mixes"]["debt funds / FDs"]["beats_loan"], 0.05)
        self.assertEqual(low["borrower_debt_tax_pct"], 34.3)                                 # the borrower's own marginal rate, from the profile
        self.assertLess(low["debt_after_tax_pct"], low["loan_rate_pct"])


if __name__ == "__main__":
    unittest.main()
