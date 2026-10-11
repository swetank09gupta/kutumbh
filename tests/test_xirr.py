import unittest
from datetime import date

from app.engine.xirr_engine import xirr


class Xirr(unittest.TestCase):
    def test_simple_doubling_in_one_year(self):
        self.assertAlmostEqual(xirr([(date(2025, 1, 1), -100), (date(2026, 1, 1), 200)]), 100.0, delta=0.2)

    def test_small_loss(self):
        self.assertAlmostEqual(xirr([(date(2020, 1, 1), -100), (date(2026, 1, 1), 94)]), -1.0, delta=0.1)

    def test_all_one_sign_has_no_rate(self):
        self.assertIsNone(xirr([(date(2025, 1, 1), -100), (date(2026, 1, 1), -5)]))

    def test_sign_changing_stream_that_newton_cannot_solve(self):
        # buy, partial sell, buy again, sell most, small remainder: several sign changes
        cfs = [(date(2020, 9, 1), -474300), (date(2020, 9, 1), 420616), (date(2021, 3, 1), -50000),
               (date(2022, 3, 1), 120000), (date(2023, 3, 1), -30000), (date(2026, 10, 1), 22942)]
        r = xirr(cfs)
        self.assertIsNotNone(r)
        t0 = date(2020, 9, 1)
        npv = sum(c / (1 + r / 100) ** ((d - t0).days / 365.25) for d, c in cfs)
        self.assertAlmostEqual(npv, 0.0, delta=50.0)      # rate is rounded to 0.01%

    def test_same_day_round_trip_nets_to_one_flow(self):
        # 4.74L bought and 4.21L sold on one day is a single 53.7k outflow; 22.9k comes back six years later
        self.assertAlmostEqual(xirr([(date(2020, 9, 1), -474300), (date(2020, 9, 1), 420616),
                                     (date(2026, 10, 1), 22942)]), -13.05, delta=0.05)

    def test_returns_none_when_no_rate_exists(self):
        # more comes back on the same day than went in, then nothing: no finite rate
        self.assertIsNone(xirr([(date(2020, 9, 1), -100), (date(2020, 9, 1), 150), (date(2020, 9, 1), -20)]))


if __name__ == "__main__":
    unittest.main()
