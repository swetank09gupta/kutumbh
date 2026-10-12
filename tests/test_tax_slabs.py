"""Slab tax per regime and year, hand-calculated; the estimator's deductions, coverage and limits."""
import unittest
from datetime import date
from decimal import Decimal as D

from app.engine import tax_slabs as ts


def total(inc, fy, regime):
    return float(ts.slab_tax(D(inc), fy, regime)["total"])


class Slabs(unittest.TestCase):
    def test_new_regime_2025_26_nil_up_to_12_lakh_by_rebate(self):
        self.assertEqual(total(1200000, "2025-26", "new"), 0.0)
        self.assertEqual(ts.slab_tax(D(1200000), "2025-26", "new")["tax_before_rebate"], D(60000))

    def test_new_regime_2025_26_just_above_the_rebate_limit_pays_the_full_slab_tax(self):
        self.assertEqual(total(1300000, "2025-26", "new"), 75000 * 1.04)               # 20k + 40k + 15k, no marginal relief modelled

    def test_new_regime_2025_26_top_slab(self):
        self.assertEqual(total(2500000, "2025-26", "new"), 330000 * 1.04)

    def test_old_regime_hand_figures(self):
        self.assertEqual(total(500000, "2025-26", "old"), 0.0)                          # 12,500 rebated
        self.assertEqual(total(1500000, "2025-26", "old"), 262500 * 1.04)               # 12.5k + 100k + 150k

    def test_new_regime_2024_25_had_different_slabs_and_rebate(self):
        self.assertEqual(total(700000, "2024-25", "new"), 0.0)
        self.assertEqual(total(900000, "2024-25", "new"), 40000 * 1.04)

    def test_2026_27_repeats_2025_26_because_budget_2026_changed_nothing(self):
        for inc in (800000, 1250000, 3000000):
            self.assertEqual(total(inc, "2026-27", "new"), total(inc, "2025-26", "new"))

    def test_a_year_with_no_verified_table_gives_no_estimate_not_a_borrowed_one(self):
        self.assertIsNone(ts.slab_tax(D(1000000), "2020-21", "old"))
        self.assertIsNone(ts.standard_deduction("2020-21", "new"))

    def test_negative_income_is_zero_tax(self):
        self.assertEqual(total(-5, "2025-26", "old"), 0.0)

    def test_every_year_is_sourced_and_slabs_are_ordered(self):
        for fy, y in ts.years().items():
            self.assertLessEqual(y["verified_on"], date.today(), fy)
            for regime in ("new", "old"):
                r = y[regime]
                self.assertRegex(r["source"], r"^https?://")
                bounds = [b for b, _ in r["slabs"]]
                self.assertIsNone(bounds[-1])
                self.assertEqual(bounds[:-1], sorted(bounds[:-1]))
                rates = [rt for _, rt in r["slabs"]]
                self.assertEqual(rates, sorted(rates))

    def test_the_current_year_must_have_an_entry(self):
        self.assertIsNone(ts.stale(date(2026, 10, 12)))
        self.assertIn("no verified income-tax slabs for FY 2027-28", ts.stale(date(2027, 5, 1)))
        self.assertIn("days ago", ts.stale(date(2027, 3, 1)))                         # FY 2026-27 verified in Oct: over 120 days by March


if __name__ == "__main__":
    unittest.main()
