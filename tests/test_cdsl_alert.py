import unittest
from datetime import date
from decimal import Decimal as D

from app.ingestion.parsers.cdsl_alert import CdslAlertParser

TEXT = ("Dear MISS AKANKSHA, Following is/are the list of transactions for your Demat account ending "
        "with *08489463 Sr. No. Company Name ISIN Quantity Debit / Credit Date and Time "
        "1 MOTILAL OSWAL AMC LTD#MOTILAL OSWAL MF-MOTILAL OSWAL LARGE AND MIDCAP FUND-DIRECT-GROWTH "
        "INF247L01999 335.064 Credit 05/10/2026 19:57:42 "
        "2 QUANT MONEY MANAGERS LTD#QUANT MF-QUANT TAX PLAN-DIRECT-GROWTH INF966L01986 13.499 Credit "
        "05/10/2026 19:57:45 For further queries, please contact your Depository Participant")


class CdslAlert(unittest.TestCase):
    def rows(self, text=TEXT):
        return CdslAlertParser().parse_text(text)

    def test_every_row_becomes_a_quantity_only_movement(self):
        a, b = self.rows()
        self.assertEqual((a.isin, a.quantity, a.transaction_type, a.date, a.kind),
                         ("INF247L01999", D("335.064"), "credit", date(2026, 10, 5), "cdsl_alert"))
        self.assertEqual((b.isin, b.quantity), ("INF966L01986", D("13.499")))
        self.assertIsNone(a.price)

    def test_identity_carries_demat_suffix_and_time(self):
        a, b = self.rows()
        self.assertIn("*08489463", a.raw_description)
        self.assertIn("19:57:42", a.raw_description)
        self.assertNotEqual(a.raw_description, b.raw_description)

    def test_debit_rows(self):
        t = TEXT.replace("Credit 05/10/2026 19:57:42", "Debit 05/10/2026 19:57:42")
        self.assertEqual(self.rows(t)[0].transaction_type, "debit")

    def test_other_text_yields_nothing(self):
        self.assertEqual(self.rows("Validated UPI and SEBI Check Tool"), [])


if __name__ == "__main__":
    unittest.main()
