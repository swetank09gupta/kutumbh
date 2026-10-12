import unittest
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

from app.engine.deposits import accrued_value, contract_rate_pct, is_cumulative, maturity_value


def fd(cur, mat, stmt=date(2026, 8, 31), end=date(2027, 8, 31), kind="fd", accrued=None):
    return SimpleNamespace(asset_type=kind, current_value=D(cur), notes=f"Maturity value: ₹{mat}" if mat else "",
                           source_statement_date=stmt, maturity_date=end, last_updated=None,
                           accrued_interest=D(accrued) if accrued is not None else None)


class Deposits(unittest.TestCase):
    def test_maturity_value_is_read_from_the_statement_note(self):
        self.assertEqual(maturity_value(fd(100000, "1,06,400.50")), D("106400.50"))     # Indian digit grouping too
        self.assertEqual(maturity_value(fd(100000, "106,400.50")), D("106400.50"))

    def test_a_cumulative_deposit_grows_along_its_contract(self):
        a = fd(100000, "106,400")
        self.assertTrue(is_cumulative(a))
        self.assertEqual(accrued_value(a, date(2026, 8, 31)), D("100000.00"))          # the statement day
        half = accrued_value(a, date(2027, 3, 1))                                       # about halfway
        self.assertTrue(D("103100") < half < D("103300"), half)
        self.assertEqual(accrued_value(a, date(2027, 8, 31)), D("106400.00"))          # at maturity
        self.assertEqual(accrued_value(a, date(2030, 1, 1)), D("106400.00"))           # never beyond it

    def test_dates_before_the_statement_do_not_shrink_the_deposit(self):
        self.assertEqual(accrued_value(fd(100000, "106,400"), date(2024, 1, 1)), D("100000"))

    def test_a_deposit_that_pays_interest_out_stays_flat(self):
        flat = fd(150000, "150,000")
        self.assertFalse(is_cumulative(flat))
        self.assertEqual(accrued_value(flat, date(2027, 1, 1)), D("150000"))
        self.assertIsNone(contract_rate_pct(flat))

    def test_the_statement_day_value_is_principal_plus_accrued_interest(self):
        # SBI 7113: principal 143,347 + interest accrued 5,142.39 on 31-08-26, maturity value 152,518 on 07-02-27
        a = fd(143347, "152,518", end=date(2027, 2, 7), accrued="5142.39")
        self.assertEqual(accrued_value(a, date(2026, 8, 31)), D("148489.39"))
        self.assertEqual(accrued_value(a, date(2027, 2, 7)), D("152518.00"))
        self.assertAlmostEqual(contract_rate_pct(a), 6.3, delta=0.4)            # the rate the statement implies

    def test_interest_paid_out_deposits_are_principal_plus_the_small_unpaid_accrual(self):
        a = fd(140000, "139,970", accrued="491.29")                              # SBI 6925: interest goes to savings
        self.assertFalse(is_cumulative(a))
        self.assertEqual(accrued_value(a, date(2026, 10, 12)), D("140491.29"))

    def test_the_rate_comes_from_the_statements_own_numbers(self):
        self.assertAlmostEqual(contract_rate_pct(fd(100000, "106,400")), 6.4, delta=0.05)

    def test_only_deposits_are_accrued(self):
        self.assertEqual(accrued_value(fd(100000, "106,400", kind="ppf"), date(2027, 3, 1)), D("100000"))

    def test_a_ppf_is_worth_exactly_its_statement_balance(self):
        ppf = SimpleNamespace(asset_type="ppf", owner_id="w", name="SBI PPF", current_value=D("1000000"), notes="",
                              source_statement_date=None, maturity_date=None, last_updated=None, accrued_interest=None)
        series = {("w", "SBI PPF"): [(date(2026, m, 28), D("1000000"), None) for m in (4, 5, 6, 7, 8)]}
        self.assertEqual(accrued_value(ppf, date.today(), series), D("1000000"))      # interest comes on 31 March, not before

    def test_a_deposit_without_a_maturity_note_is_left_alone(self):
        self.assertEqual(accrued_value(fd(100000, None), date(2027, 3, 1)), D("100000"))


if __name__ == "__main__":
    unittest.main()
