import unittest
from datetime import date
from decimal import Decimal as D

from app.engine.small_savings import parse_rates, ppf_accrued_from_balances, ppf_balance

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


class PpfAccrued(unittest.TestCase):
    def test_the_interest_since_31_march_is_added_to_the_statement_balance(self):
        # 10,00,000 all year; by 12 Oct 2026 six months (Apr..Sep) have accrued at 7.1% / 12
        snaps = [(date(2026, m, 28 if m == 2 else 30), D("1000000")) for m in (4, 6, 9)] + [(date(2026, 8, 31), D("1000000"))]
        got = ppf_accrued_from_balances(snaps, date(2026, 10, 12), lambda d: 7.1)
        self.assertAlmostEqual(float(got), 1000000 * 0.071 / 12 * 6, delta=2.0)

    def test_before_april_the_year_in_progress_is_the_one_that_began_last_april(self):
        got = ppf_accrued_from_balances([(date(2026, 1, 31), D("1000000"))], date(2026, 2, 10), lambda d: 7.1)
        self.assertAlmostEqual(float(got), 1000000 * 0.071 / 12 * 10, delta=2.0)             # Apr..Jan completed

    def test_a_deposit_late_in_a_month_does_not_earn_that_month(self):
        snaps = [(date(2026, 4, 30), D("1000000")), (date(2026, 5, 31), D("1100000"))]       # 1,00,000 paid during May
        got = ppf_accrued_from_balances(snaps, date(2026, 6, 10), lambda d: 7.1)
        self.assertAlmostEqual(float(got), 1000000 * 0.071 / 12 * 2, delta=2.0)               # May earns on the lower balance

    def test_nothing_without_statements(self):
        self.assertEqual(ppf_accrued_from_balances([], date(2026, 10, 12)), D(0))


if __name__ == "__main__":
    unittest.main()
