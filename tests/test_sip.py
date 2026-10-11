import unittest
from collections import defaultdict
from datetime import date
from types import SimpleNamespace as NS

from app.engine import sip


def run(trades, today=date(2026, 10, 11), months=6):
    """Drive monthly_sip_by_fund with an in-memory trade list."""
    rows = [NS(owner_id="u", isin=i, trade_date=d, amount=a, id=str(n), side="B", quantity=1, price=a, source="coin",
               instrument_name=i) for n, (i, d, a) in enumerate(trades)]
    sip._buys = lambda db, owners: rows
    return sip.monthly_sip_by_fund(None, ["u"], months, today)


class Detection(unittest.TestCase):
    def tearDown(self):
        import importlib
        importlib.reload(sip)

    def test_steady_monthly_buys_are_a_sip(self):
        t = [("INF1", date(2026, m, 5), 10000) for m in (5, 6, 7, 8, 9, 10)]
        self.assertEqual(run(t), {("u", "INF1"): 10000})

    def test_one_lump_month_does_not_hide_a_live_sip(self):
        t = [("INF1", date(2026, 8, 5), 30998), ("INF1", date(2026, 9, 5), 10000), ("INF1", date(2026, 10, 5), 10000)]
        self.assertEqual(run(t), {("u", "INF1"): 10000})

    def test_skipped_months_still_count_when_current_cadence_holds(self):
        t = [("INF1", date(2026, 4, 2), 10000), ("INF1", date(2026, 5, 4), 10000),
             ("INF1", date(2026, 9, 2), 10000), ("INF1", date(2026, 10, 5), 10000)]
        self.assertEqual(run(t), {("u", "INF1"): 10000})

    def test_a_single_lump_sum_is_not_a_sip(self):
        self.assertEqual(run([("INF1", date(2026, 10, 5), 24980)]), {})

    def test_a_stopped_sip_is_not_current(self):
        t = [("INF1", date(2026, 4, 5), 10000), ("INF1", date(2026, 5, 5), 10000)]
        self.assertEqual(run(t), {})

    def test_wildly_varying_buys_are_discretionary(self):
        t = [("INF1", date(2026, 8, 5), 1000), ("INF1", date(2026, 9, 5), 50000), ("INF1", date(2026, 10, 5), 9000)]
        self.assertEqual(run(t), {})


if __name__ == "__main__":
    unittest.main()
