import unittest
from datetime import date
from decimal import Decimal as D
from unittest import mock

from app.ledger import derive
from app.ledger.rules import Movement

ISIN = "INF000A01010"


class FakeTxn:
    def __init__(self, i, d, amt, kind="debit", desc="ACHDr INDIAN CLEARIN"):
        self.id, self.date, self.amount, self.transaction_type, self.raw_description = i, d, D(amt), kind, desc
        self.fingerprint = f"fp{i}"


def nav_table(day_to_nav):
    def f(isin, d):
        for back in range(0, 8):
            from datetime import timedelta
            x = d - timedelta(days=back)
            if x in day_to_nav:
                return x, D(day_to_nav[x])
        return None
    return f


class PriceIt(unittest.TestCase):
    NAV = {date(2026, 9, 1): "38.0", date(2026, 9, 2): "38.5", date(2026, 9, 3): "39.0"}      # Tue..Thu

    def test_bank_debit_pins_the_nav_day(self):
        line = Movement("m", ISIN, date(2026, 9, 4), D("300"), D(0))                     # 300 units credited Fri
        bank = [FakeTxn(1, date(2026, 9, 1), "11550")]                                   # 300 x 38.5
        with mock.patch.object(derive, "nav_on_or_before", nav_table(self.NAV)):
            got = derive._price_it(ISIN, line, bank, set())
        self.assertEqual((got["date"], got["nav"], got["amount"]), (date(2026, 9, 2), D("38.5"), D("11550")))
        self.assertIs(got["bank"], bank[0])

    def test_without_bank_evidence_it_uses_the_previous_business_day_nav(self):
        line = Movement("m", ISIN, date(2026, 9, 4), D("300"), D(0))
        with mock.patch.object(derive, "nav_on_or_before", nav_table(self.NAV)):
            got = derive._price_it(ISIN, line, [], set())
        self.assertEqual((got["date"], got["bank"]), (date(2026, 9, 3), None))
        self.assertEqual(got["amount"], D("11700.00"))

    def test_one_bank_debit_confirms_one_line_only(self):
        a = Movement("a", ISIN, date(2026, 9, 4), D("300"), D(0))
        b = Movement("b", ISIN, date(2026, 9, 4), D("300"), D(0))
        bank, used = [FakeTxn(1, date(2026, 9, 1), "11550")], set()
        with mock.patch.object(derive, "nav_on_or_before", nav_table(self.NAV)):
            first = derive._price_it(ISIN, a, bank, used)
            second = derive._price_it(ISIN, b, bank, used)
        self.assertIsNotNone(first["bank"])
        self.assertIsNone(second["bank"])

    def test_missing_nav_means_no_trade_is_invented(self):
        line = Movement("m", ISIN, date(2026, 9, 4), D("300"), D(0))
        with mock.patch.object(derive, "nav_on_or_before", lambda i, d: None):
            self.assertIsNone(derive._price_it(ISIN, line, [], set()))


if __name__ == "__main__":
    unittest.main()
