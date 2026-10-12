"""Amortisation maths, checked against figures HDFC itself printed (synthetic only where noted)."""
import unittest
from datetime import date
from decimal import Decimal as D

from app.engine import loan_schedule as s


class Maths(unittest.TestCase):
    def test_reproduces_the_lenders_next_figure_to_the_rupee(self):
        # HDFC letter 1 Sep 2026: 11,222,991 after prepayment, EMI 170,000 @ 7.1%. Letter of 1 Oct: 10,619,394 after a
        # 500,000 prepayment, i.e. 11,119,394 before it.
        _i, _p, after = s.step(D(11222991), 7.1, D(170000))
        self.assertLess(abs(after - D(11119394)), D("1"))

    def test_emis_left_matches_the_lenders_count(self):
        self.assertEqual(s.payoff(D(10619394), 7.1, D(170000))["months"], 79)           # letter of 1 Oct 2026: 79 EMIs
        self.assertEqual(s.payoff(D(11222991), 7.1, D(170000))["months"], 84)           # letter of 1 Sep 2026: 84
        self.assertEqual(s.payoff(D(12803080), 7.1, D(164056))["months"], 105)          # letter of 1 Jul 2026: 105

    def test_the_last_instalment_is_the_smaller_remainder(self):
        r = s.payoff(D(1000), 12, D(400))
        self.assertEqual(r["months"], 3)
        self.assertLess(r["last_emi"], D(400))
        self.assertEqual(r["interest"], D("18.26"))            # 10.00 + 6.10 + 2.161

    def test_an_emi_below_the_interest_never_pays_off(self):
        self.assertIsNone(s.payoff(D(1000000), 12, D(5000)))

    def test_nothing_owed_is_zero_instalments(self):
        self.assertEqual(s.payoff(D(0), 7.1, D(100))["months"], 0)

    def test_emi_for_round_trips(self):
        for n in (60, 180, 326):
            emi = s.emi_for(D(5000000), 8.35, n)
            self.assertIn(s.payoff(D(5000000), 8.35, emi)["months"], (n, n + 1))       # rounding the EMI may leave pennies

    def test_months_clamp_to_month_end(self):
        self.assertEqual(s.add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(s.add_months(date(2026, 11, 5), 2), date(2027, 1, 5))
        self.assertEqual(s.payoff_date(date(2026, 11, 5), 78), date(2033, 4, 5))

    def test_a_prepayment_saves_interest_and_instalments(self):
        r = s.interest_saved(D(11125338), D(500000), 7.1, D(170000))
        self.assertGreater(r["interest_saved"], D(100000))
        self.assertGreaterEqual(r["months_saved"], 1)
        self.assertEqual(s.interest_saved(D(11125338), D(0), 7.1, D(170000))["interest_saved"], D("0.00"))

    def test_a_higher_rate_costs_more_interest_over_the_same_loan(self):
        a, b = s.payoff(D(10000000), 7.10, D(170000)), s.payoff(D(10000000), 7.35, D(170000))
        self.assertGreater(b["interest"], a["interest"])
        self.assertGreaterEqual(b["months"], a["months"])


if __name__ == "__main__":
    unittest.main()
