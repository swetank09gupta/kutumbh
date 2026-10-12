"""The SBI statement's FD table: the Interest Accrued column must be read (it was once thrown away)."""
import unittest
from datetime import date
from decimal import Decimal as D

from app.ingestion.parsers.sbi import SbiParser

TABLE = """As on 31-08-26
FIXED DEPOSITS
TDR AND STDR ACCOUNTS CURRENCY: INR
TERM DEPOSIT XXXXXXX7113 07-02-25 143347.00 P SINGLE 6.25 0.00 5142.39 152518.00 07-02-27 Yes
TERM DEPOSIT XXXXXXX6925 11-10-24 140000.00 P SINGLE 6.25 0.00 491.29 139970.00 11-10-26 Yes
TERM DEPOSIT XXXXX6925 11-10-24 200000.00 P 6.80 0.00 763.43 200000.00 11-10-25
ACCOUNT(S) CLOSED DURING THE MONTH
"""


class SbiFd(unittest.TestCase):
    def setUp(self):
        self.assets, *_ = SbiParser()._extract_fd_ppf(TABLE, "wife")
        self.by = {a.name + str(a.current_value): a for a in self.assets}

    def test_principal_accrued_interest_and_maturity_value_are_all_kept(self):
        a = self.by["SBI FD ...7113143347.00"]
        self.assertEqual((a.current_value, a.accrued_interest, a.maturity_amount), (D("143347.00"), D("5142.39"), D("152518.00")))
        self.assertEqual((a.start_date, a.maturity_date), (date(2025, 2, 7), date(2027, 2, 7)))

    def test_both_statement_layouts_are_read(self):
        old = self.by["SBI FD ...6925200000.00"]                                  # 2025 layout, no mode word
        new = self.by["SBI FD ...6925140000.00"]                                  # 2026 layout
        self.assertEqual((old.accrued_interest, new.accrued_interest), (D("763.43"), D("491.29")))


if __name__ == "__main__":
    unittest.main()
