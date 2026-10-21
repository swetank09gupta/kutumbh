"""The data side of planning: how holdings are classified, what counts as retirement living cost, loan payoff, prepay-vs-invest."""
import unittest
from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd

from app.planning import allocation, need, plan
from app.planning.assumptions import Assumptions


class Classify(unittest.TestCase):
    def test_holdings_land_in_the_right_asset(self):
        c = allocation.classify
        self.assertEqual(c("UTI NNX50INDF D-GROW", "mutual_fund"), {"equity": 1.0})
        self.assertEqual(c("HDFC Liquid Fund", "mutual_fund"), {"debt": 1.0})
        self.assertEqual(c("SBI Gold Fund", "mutual_fund"), {"gold": 1.0})
        self.assertEqual(c("GOI SGB 33076 2031", "bonds"), {"gold": 1.0})              # sovereign gold bond
        self.assertEqual(c("7.26% GOI 2033", "bonds"), {"debt": 1.0})                  # a plain government bond
        self.assertEqual(c("Quant Multi Asset Fund", "mutual_fund"), {"equity": 0.6, "debt": 0.4})
        self.assertEqual(c("SBI PPF", None, "ppf"), {"ppf": 1.0})
        self.assertEqual(c("SBI FD ...6925", None, "fd"), {"debt": 1.0})
        self.assertEqual(c("TATA STEEL LIMITED", "equity"), {"equity": 1.0})

    def test_the_blend_is_value_weighted_and_ignores_empty_items(self):
        mix = allocation.blend([(300.0, {"equity": 1.0}), (100.0, {"debt": 1.0}), (0.0, {"gold": 1.0})])
        self.assertEqual(mix, {"equity": 0.75, "debt": 0.25})
        self.assertEqual(allocation.blend([]), {})

    def test_long_money_takes_equity_near_money_does_not_and_emergency_never(self):
        r = allocation.recommended_equity
        self.assertGreater(r(15), r(8)); self.assertGreater(r(8), r(4)); self.assertGreater(r(4), r(1))
        self.assertEqual(r(15, goal_type="emergency"), 0.0)
        self.assertEqual(round(r(15, "aggressive") - r(15, "moderate"), 2), 0.10)
        self.assertEqual(round(r(15, "conservative") - r(15, "moderate"), 2), -0.10)
        mix = allocation.split_for(r(15))
        self.assertAlmostEqual(sum(mix.values()), 1.0)


class Spending(unittest.TestCase):
    ROWS = [("emi", "emi_regular", 1_500_000), ("insurance", "term_insurance", 500_000), ("insurance", None, 10_000),
            ("child_expenses", "education_care", 300_000), ("family_support", None, 250_000),
            ("other", None, 900_000), ("food", "groceries", 40_000), ("shopping", "online", 300_000)]

    def test_retirement_core_drops_what_ends_or_is_funded_elsewhere(self):
        out = need.split_spending(self.ROWS)
        self.assertEqual(out["total"], 3_800_000)
        self.assertEqual(out["retirement_core"], 3_800_000 - 1_500_000 - 500_000 - 300_000 - 250_000)
        self.assertEqual({e["category"] for e in out["excluded"]}, {"emi", "insurance", "child_expenses", "family_support"})
        self.assertNotIn(10_000, [e["amount"] for e in out["excluded"]])               # health-style insurance stays in

    def test_the_window_is_twelve_complete_months(self):
        self.assertEqual(need.month_window(date(2026, 10, 21)), (date(2025, 10, 1), date(2026, 10, 1)))
        self.assertEqual(need.month_window(date(2026, 1, 5)), (date(2025, 1, 1), date(2026, 1, 1)))


class Loan(unittest.TestCase):
    def test_a_loan_clears_in_its_term_and_prepaying_shortens_it(self):
        emi = 100000 * 0.01 / (1 - 1.01 ** -12)                                        # ₹1 lakh at 12% a year over 12 months
        self.assertEqual(plan.loan_payoff(100000, 12, emi)["months"], 12)
        faster = plan.loan_payoff(100000, 12, emi, extra=5000)
        self.assertLess(faster["months"], 12)
        self.assertLess(faster["interest"], plan.loan_payoff(100000, 12, emi)["interest"])

    def test_the_hdfc_letter_case(self):
        # ₹1,06,19,394 at 7.1% with the ₹1.7 lakh EMI: the lender's letter says 79 instalments left
        self.assertEqual(plan.loan_payoff(10619394, 7.1, 170000)["months"], 79)


class PrepayVsInvest(unittest.TestCase):
    def data(self):
        idx = pd.date_range("2011-10-31", periods=181, freq="ME")
        rng = np.random.default_rng(3)
        j = pd.DataFrame({k: 0.005 + v * rng.standard_normal(len(idx)) for k, v in (("equity", 0.045), ("gold", 0.04), ("debt", 0.01))}, index=idx)
        return {"joint": j, "equity_long": j["equity"]}

    def test_a_higher_loan_rate_is_harder_to_beat_and_debt_rarely_beats_it(self):
        a = replace(Assumptions(), n_paths=1500)
        low = plan.prepay_vs_invest(self.data(), a, 6.0, years=(10,))[0]["mixes"]
        high = plan.prepay_vs_invest(self.data(), a, 9.0, years=(10,))[0]["mixes"]
        for mix in low:
            self.assertGreaterEqual(low[mix]["beats_loan"], high[mix]["beats_loan"])
        self.assertLess(high["100% debt"]["beats_loan"], 0.05)                         # debt after tax is about 5%
        self.assertGreater(low["100% equity"]["beats_loan"], low["100% debt"]["beats_loan"])


class Route(unittest.TestCase):
    def test_the_plan_route_exists(self):
        from app.main import app
        self.assertIn("/api/plan/", {getattr(r, "path", "") for r in app.routes})


if __name__ == "__main__":
    unittest.main()
