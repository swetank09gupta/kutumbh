import unittest
from datetime import date
from decimal import Decimal as D

from app.ingestion.parsers.zerodha import ZerodhaParser

TD = date(2022, 6, 3)

NOTE_2022 = """Equity
1200000017339699 14:48:03 53252601 14:58:41 LICI - EQ / B NSE 2 801.00 801.00 (1602.00)
INE0J1Y01017
Sub total 2 (1602.00)
"""

NOTE_2024 = """Equity
1100000016178195 10:08:52 31203013 12:55:40 EMUDHRA-EQ/INE01QM01018 B NSE 3 875.00 875 (2625.00)
Sub Total 3 (2625.00)
"""

NOTE_SELL = """1100000016178196 10:08:52 31203014 12:55:40 TATASTEEL-EQ/INE081A01020 S NSE 1,000 150.55 150.55 150,550.00
1100000016178197 10:09:52 31203015 12:56:40 TATASTEEL-EQ/INE081A01020 S NSE 20 150.55 150.55 3,011.00
"""


class LegacyContractNoteText(unittest.TestCase):
    def parse(self, text):
        return ZerodhaParser()._parse_contract_note_text(text, TD)

    def test_2022_layout_isin_on_following_line(self):
        (t,) = self.parse(NOTE_2022)
        self.assertEqual((t.isin, t.counterparty, t.quantity, t.price), ("INE0J1Y01017", "LICI", D(2), D("801.00")))
        self.assertEqual((t.transaction_type, t.amount, t.reference_number, t.kind),
                         ("debit", D("1602.00"), "53252601", "contract_note"))

    def test_2024_layout_isin_inside_security(self):
        (t,) = self.parse(NOTE_2024)
        self.assertEqual((t.isin, t.counterparty, t.quantity, t.amount), ("INE01QM01018", "EMUDHRA", D(3), D("2625.00")))

    def test_sell_without_parentheses_and_thousands_separators(self):
        a, b = self.parse(NOTE_SELL)
        self.assertEqual((a.transaction_type, a.quantity, a.amount), ("credit", D(1000), D("150550.00")))
        self.assertEqual(b.quantity, D(20))
        self.assertNotEqual(a.reference_number, b.reference_number)

    def test_repeated_line_is_not_duplicated(self):
        self.assertEqual(len(self.parse(NOTE_2024 + NOTE_2024)), 1)

    def test_non_trade_lines_ignored(self):
        self.assertEqual(self.parse("Net total (1602.00)\nSub total 2 (1602.00)\n"), [])


if __name__ == "__main__":
    unittest.main()


class Old2021HoldingsLayout(unittest.TestCase):
    TEXT = """HOLDING AS ON 30/09/2021
ISIN Code Company Name Curr Bal Free Bal Pldg.BalEarmark.Bal DematRemat LockIn Rate Value
INF209K01UN8 ABSL TAX96 DP GROWTH 359.150 0.000 359.150 46.30 16628.65
INF846K01S29 AXIS NF100IND D-GROW 295.863 295.863 15.31 4528.48
INF179K01YS4 HDFC TSDP GR 17.485 0.000 17.485 765.16 13378.88
Total: 1203.169 295.863 907.306 52473.74
"""

    def test_units_rate_value_are_taken_positionally(self):
        hs = ZerodhaParser()._parse_old_layout_holdings(self.TEXT)
        self.assertEqual([(h.isin, h.units, h.current_nav, h.current_value) for h in hs], [
            ("INF209K01UN8", D("359.150"), D("46.30"), D("16628.65")),
            ("INF846K01S29", D("295.863"), D("15.31"), D("4528.48")),
            ("INF179K01YS4", D("17.485"), D("765.16"), D("13378.88")),
        ])

    def test_total_line_and_headers_are_ignored(self):
        self.assertEqual(len(ZerodhaParser()._parse_old_layout_holdings(self.TEXT)), 3)

    def test_no_marker_no_holdings(self):
        self.assertEqual(ZerodhaParser()._parse_old_layout_holdings("nothing here"), [])
