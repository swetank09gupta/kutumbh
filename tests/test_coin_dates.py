import unittest
from datetime import date
from pathlib import Path

from app.ingestion.parsers.zerodha_coin import ZerodhaCoinParser


class CoinFilenameDate(unittest.TestCase):
    def d(self, name):
        return ZerodhaCoinParser._extract_date_from_filename(Path(name))

    def test_weekday_selects_the_year(self):
        self.assertEqual(self.d("Mon,_05_Oct_coin_x.html"), date(2026, 10, 5))
        self.assertEqual(self.d("Tue,_06_May_coin_x.html"), date(2025, 5, 6))
        self.assertEqual(self.d("Wed,_04_Aug_coin_x.html"), date(2021, 8, 4))

    def test_future_date_in_current_year_falls_back_to_earlier_year(self):
        # 22 Mar was a Tuesday in 2022; must not be dated into the current year
        self.assertEqual(self.d("Tue,_22_Mar_coin_x.html"), date(2022, 3, 22))

    def test_unparseable_name(self):
        self.assertIsNone(self.d("statement.html"))


if __name__ == "__main__":
    unittest.main()
