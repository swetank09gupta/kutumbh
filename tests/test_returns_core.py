import unittest
from datetime import date

from app.engine.portfolio_xirr import Position, _xirr_with_balances, summarise

TODAY = date(2026, 10, 12)


def P(name, current, cfs, covered=True, cls="mutual_fund", closed=False):
    return Position(owner="u", isin=name, name=name, cls=cls, current=current, cfs=cfs, covered=covered, closed=closed)


class Summarise(unittest.TestCase):
    def test_return_counts_only_positions_with_known_cost(self):
        good = P("a", 120.0, [(date(2025, 10, 12), -100.0)])
        blind = P("b", 5000.0, [], covered=False)                 # units with no purchase history
        r = summarise([good, blind], TODAY)
        self.assertAlmostEqual(r["xirr_pct"], 20.0, delta=0.3)     # the blind position does not inflate it
        self.assertEqual(r["current"], 120.0)
        self.assertEqual(r["current_all"], 5120.0)
        self.assertAlmostEqual(r["coverage_pct"], 2.3, delta=0.1)
        self.assertIn("no purchase history", r["note"])

    def test_realised_trades_of_closed_positions_count(self):
        closed = P("c", 0.0, [(date(2024, 10, 12), -100.0), (date(2025, 10, 12), 150.0)], closed=True)
        held = P("d", 110.0, [(date(2025, 10, 12), -100.0)])
        r = summarise([closed, held], TODAY)
        self.assertEqual((r["invested"], r["proceeds"], r["gain"]), (200.0, 150.0, 60.0))
        self.assertEqual(r["coverage_pct"], 100.0)
        self.assertIsNone(r["note"])

    def test_classes_add_up_to_the_total(self):
        ps = [P("a", 120.0, [(date(2025, 10, 12), -100.0)]),
              P("b", 55.0, [(date(2025, 4, 12), -50.0)], cls="equity")]
        total = summarise(ps, TODAY)
        parts = [summarise([p], TODAY) for p in ps]
        for k in ("invested", "current", "gain"):
            self.assertAlmostEqual(total[k], sum(x[k] for x in parts), places=2)

    def test_no_rate_fits_still_reports_the_gain(self):
        r = summarise([P("e", 1.0, [(date(2026, 10, 12), -100.0), (date(2026, 10, 12), 90.0)])], TODAY)
        self.assertIsNone(r["xirr_pct"])
        self.assertEqual(r["gain"], -9.0)
        self.assertIn("plain gain", r["note"])

    def test_churned_money_gets_no_annual_rate(self):
        # bought and sold within 3 days, 20% profit: annualised that is a huge meaningless number
        r = summarise([P("f", 0.0, [(date(2026, 9, 1), -100.0), (date(2026, 9, 4), 120.0)], closed=True)], TODAY)
        self.assertIsNone(r["xirr_pct"])
        self.assertEqual(r["gain"], 20.0)
        self.assertIn("under a year", r["note"])

    def test_eight_months_is_plain_gain_not_an_annual_rate(self):
        r = summarise([P("h", 120.0, [(date(2026, 2, 12), -100.0)])], TODAY)
        self.assertIsNone(r["xirr_pct"])
        self.assertEqual(r["gain_pct"], 20.0)

    def test_money_held_for_years_keeps_its_annual_rate(self):
        r = summarise([P("g", 150.0, [(date(2023, 10, 12), -100.0)])], TODAY)
        self.assertAlmostEqual(r["xirr_pct"], 14.5, delta=0.5)


class Deposits(unittest.TestCase):
    def test_a_deposit_is_a_balance_not_a_return(self):
        d = Position(owner='u', isin=None, name='SBI FD', cls='fd', current=260000.0, covered=False, deposit=True)
        r = summarise([d], TODAY)
        self.assertIsNone(r['xirr_pct'])
        self.assertEqual((r['invested'], r['current_all']), (0, 260000.0))


class WithBalances(unittest.TestCase):
    def test_a_balance_at_its_contract_rate_pulls_the_whole_return_toward_that_rate(self):
        market = P("m", 150.0, [(date(2025, 10, 12), -100.0)])                       # +50% in a year
        ppf = Position(owner="u", isin=None, name="PPF", cls="ppf", current=1000.0, covered=False, deposit=True, rate=7.1)
        measured = summarise([market], TODAY)["xirr_pct"]
        whole = _xirr_with_balances([market, ppf], TODAY)
        self.assertGreater(measured, 45)
        self.assertTrue(7.0 < whole < 15.0, whole)          # dominated by the 1,000 earning 7.1%

    def test_a_balance_without_a_rate_is_left_out_not_guessed(self):
        market = P("m", 150.0, [(date(2025, 10, 12), -100.0)])
        nameless = Position(owner="u", isin=None, name="X", cls="fd", current=1000.0, covered=False, deposit=True, rate=None)
        self.assertEqual(_xirr_with_balances([market, nameless], TODAY), summarise([market], TODAY)["xirr_pct"])


if __name__ == "__main__":
    unittest.main()
