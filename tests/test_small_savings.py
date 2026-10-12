import unittest
from datetime import date
from decimal import Decimal as D

from app.engine.small_savings import parse_rates, ppf_balance

TABLE = ("Interest Rate Since Inception YEAR RATE OF INTEREST (%) 01.07.2019 TO 31.03.2020 7.9 "
         "01.04.2020 TO 30.09.2026 7.1 01.10.2018 TO 31.06.2019 8.0 Back to Top")


class Rates(unittest.TestCase):
    def test_table_text_becomes_dated_rates_even_with_an_impossible_date(self):
        self.assertEqual(parse_rates(TABLE), [("2018-10-01", 8.0), ("2019-07-01", 7.9), ("2020-04-01", 7.1)])


class PpfRule(unittest.TestCase):
    flat = staticmethod(lambda d: 7.1)

    def test_a_deposit_on_or_before_the_5th_earns_that_month(self):
        # 1,00,000 on 3 Apr 2025; by 1 Jun 2025 two completed months (Apr, May) have accrued
        bal, accr = ppf_balance([(date(2025, 4, 3), D(100000))], date(2025, 6, 1), self.flat)
        self.assertEqual(bal, D("100000.00"))
        self.assertAlmostEqual(float(accr), 100000 * 0.071 / 12 * 2, places=1)

    def test_a_deposit_after_the_5th_earns_from_next_month(self):
        _, accr = ppf_balance([(date(2025, 4, 20), D(100000))], date(2025, 6, 1), self.flat)
        self.assertAlmostEqual(float(accr), 100000 * 0.071 / 12, places=1)       # May only

    def test_interest_is_credited_on_31_march_and_then_compounds(self):
        bal, accr = ppf_balance([(date(2024, 4, 1), D(100000))], date(2025, 6, 1), self.flat)
        yr1 = 100000 * 0.071 / 12 * 12                       # April..March
        self.assertAlmostEqual(float(bal), 100000 + yr1, places=0)               # credited at 31 Mar 2025
        self.assertAlmostEqual(float(accr), (100000 + yr1) * 0.071 / 12 * 2, delta=2.0)


if __name__ == "__main__":
    unittest.main()
