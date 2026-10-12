"""Capital gains: classification, FIFO, term boundaries, rates by sale date, set-off, exemption (hand-calculated cases)."""
import unittest
from datetime import date
from decimal import Decimal as D

from app.engine import tax_gains as tg
from app.ledger.rules import Trade


def T(day, side, qty, price, isin="INE000A01010", i=[0]):
    i[0] += 1
    return Trade(id=str(i[0]), isin=isin, trade_date=day, side=side, qty=D(qty), price=D(price))


NAMES = {"INE000A01010": "ACME", "INF000A01010": "SOME EQUITY FUND", "INF209KB19F6": "SILVER", "INF000D01010": "SOME DEBT FUND"}
CATS = {"INF000A01010": "Equity Scheme - Large Cap Fund", "INF209KB19F6": "Exchange Traded Funds (ETFs) - Equity ETF",
        "INF000D01010": "Debt Scheme - Liquid Fund"}


class Classification(unittest.TestCase):
    def test_shares_funds_etfs_and_unknowns(self):
        self.assertEqual(tg.tax_class("INE213A01029", "ONGC"), "equity_share")
        self.assertEqual(tg.tax_class("INF200K01RT0", "SBI Tech", "Equity Scheme - Sectoral/ Thematic"), "equity_fund")
        self.assertEqual(tg.tax_class("INF846K01S29", "Axis Nifty 100 Index Fund", "Other Scheme - Index Funds"), "equity_fund")
        self.assertEqual(tg.tax_class("INF209KB19F6", "SILVER", "Exchange Traded Funds (ETFs) - Equity ETF"), "gold_silver_etf")   # AMFI's label is wrong
        self.assertEqual(tg.tax_class("INF000D01010", "X", "Debt Scheme - Liquid Fund"), "debt_fund")
        self.assertEqual(tg.tax_class("INF111Z01010", "Mystery", None), "unclassified")          # never guessed
        self.assertEqual(tg.tax_class("IN0020230069", "SGB"), "other")

    def test_financial_year_edges(self):
        self.assertEqual((tg.fy_of(date(2026, 3, 31)), tg.fy_of(date(2026, 4, 1))), ("2025-26", "2026-27"))


class Fifo(unittest.TestCase):
    def run_fifo(self, trades, isin="INE000A01010"):
        return tg.fifo(trades, NAMES, CATS)

    def test_the_oldest_lot_is_sold_first_and_a_lot_can_be_split(self):
        r = self.run_fifo([T(date(2024, 1, 1), "B", 10, 100), T(date(2024, 6, 1), "B", 10, 200), T(date(2025, 2, 1), "S", 15, 300)])
        a, b = r.sales
        self.assertEqual((a.qty, a.cost, a.proceeds), (D(10), D(1000), D(3000)))
        self.assertEqual((b.qty, b.cost, b.proceeds), (D(5), D(1000), D(1500)))
        self.assertEqual(r.open_lots["INE000A01010"][0].qty, D(5))

    def test_long_term_needs_more_than_twelve_months(self):
        buy = T(date(2025, 1, 15), "B", 1, 100)
        self.assertEqual(self.run_fifo([buy, T(date(2026, 1, 15), "S", 1, 150)]).sales[0].term, "short")      # exactly 12 months
        self.assertEqual(self.run_fifo([buy, T(date(2026, 1, 16), "S", 1, 150)]).sales[0].term, "long")

    def test_a_buy_and_sell_on_the_same_day_buys_first(self):
        r = self.run_fifo([T(date(2025, 5, 1), "S", 5, 120), T(date(2025, 5, 1), "B", 5, 100)])
        self.assertEqual(len(r.sales), 1)
        self.assertEqual(r.unknown_cost, [])

    def test_units_sold_with_no_purchase_on_file_are_named_not_costed(self):
        r = self.run_fifo([T(date(2025, 1, 1), "B", 4, 100), T(date(2025, 3, 1), "S", 10, 150)])
        self.assertEqual(r.sales[0].qty, D(4))
        self.assertEqual(r.unknown_cost[0]["units"], 6.0)


class YearSummary(unittest.TestCase):
    def fy(self, trades, fy):
        return tg.year_summary(tg.fifo(trades, NAMES, CATS).sales, fy)

    def test_rates_follow_the_sale_date_across_the_july_2024_change(self):
        for sale, stcg_rate in ((date(2024, 7, 22), 15), (date(2024, 7, 23), 20)):
            r = self.fy([T(date(2024, 1, 2), "B", 100, 100), T(sale, "S", 100, 200)], "2024-25")      # 10,000 short-term gain
            self.assertAlmostEqual(r["equity"]["tax_stcg"], 10000 * stcg_rate / 100, places=2, msg=str(sale))

    def test_ltcg_exemption_and_rate(self):
        r = self.fy([T(date(2023, 1, 2), "B", 1000, 100), T(date(2025, 6, 2), "S", 1000, 300)], "2025-26")   # 200,000 long-term gain
        e = r["equity"]
        self.assertEqual((e["ltcg"], e["ltcg_exemption"], e["ltcg_taxable"]), (200000.0, 125000.0, 75000.0))
        self.assertAlmostEqual(e["tax_ltcg"], 75000 * 0.125, places=2)

    def test_under_the_exemption_nothing_is_due(self):
        r = self.fy([T(date(2023, 1, 2), "B", 100, 100), T(date(2025, 6, 2), "S", 100, 300)], "2025-26")    # 20,000
        self.assertEqual((r["equity"]["tax_ltcg"], r["estimated_tax"]), (0.0, 0.0))

    def test_a_short_term_loss_sets_off_against_a_long_term_gain(self):
        t = [T(date(2023, 1, 2), "B", 100, 100), T(date(2025, 6, 2), "S", 100, 1500),                     # long gain 140,000
             T(date(2025, 5, 1), "B", 100, 100, isin="INF000A01010"), T(date(2025, 8, 1), "S", 100, 50, isin="INF000A01010")]  # short loss 5,000
        e = self.fy(t, "2025-26")["equity"]
        self.assertEqual((e["stcg"], e["ltcg"]), (0.0, 135000.0))

    def test_a_long_term_loss_is_carried_forward_not_set_off_against_short_term_gains(self):
        t = [T(date(2023, 1, 2), "B", 100, 500), T(date(2025, 6, 2), "S", 100, 100),                       # long loss 40,000
             T(date(2025, 5, 1), "B", 100, 100, isin="INF000A01010"), T(date(2025, 8, 1), "S", 100, 150, isin="INF000A01010")]  # short gain 5,000
        e = self.fy(t, "2025-26")["equity"]
        self.assertEqual((e["stcg"], e["ltcg"], e["loss_carried_forward"]), (5000.0, 0.0, 40000.0))

    def test_gold_silver_etf_is_12_5_percent_without_the_exemption_after_july_2024(self):
        t = [T(date(2024, 1, 2), "B", 100, 100, isin="INF209KB19F6"), T(date(2025, 8, 2), "S", 100, 400, isin="INF209KB19F6")]   # 30,000
        r = self.fy(t, "2025-26")["gold_silver_etf"]
        self.assertEqual((r["ltcg"], r["tax_ltcg"]), (30000.0, 3750.0))

    def test_gold_etf_sold_before_july_2024_is_reported_not_computed(self):
        t = [T(date(2022, 1, 2), "B", 10, 100, isin="INF209KB19F6"), T(date(2024, 2, 2), "S", 10, 200, isin="INF209KB19F6")]
        r = self.fy(t, "2023-24")
        self.assertEqual(r["gold_silver_etf"]["tax_ltcg"], 0.0)
        self.assertIn("36-month rule", r["not_computed"][0]["reason"])

    def test_debt_fund_is_slab_when_bought_after_april_2023_and_not_computed_before(self):
        new = [T(date(2023, 6, 1), "B", 10, 100, isin="INF000D01010"), T(date(2025, 2, 2), "S", 10, 130, isin="INF000D01010")]
        self.assertEqual(self.fy(new, "2024-25")["debt_funds_at_slab"], 300.0)
        old = [T(date(2022, 6, 1), "B", 10, 100, isin="INF000D01010"), T(date(2025, 2, 2), "S", 10, 130, isin="INF000D01010")]
        r = self.fy(old, "2024-25")
        self.assertEqual(r["debt_funds_at_slab"], 0.0)
        self.assertEqual(len(r["not_computed"]), 1)

    def test_only_the_asked_financial_year_is_counted(self):
        t = [T(date(2024, 1, 2), "B", 10, 100), T(date(2025, 3, 31), "S", 5, 200), T(date(2025, 4, 1), "S", 5, 200)]
        s = tg.fifo(t, NAMES, CATS).sales
        self.assertEqual((tg.year_summary(s, "2024-25")["sales"], tg.year_summary(s, "2025-26")["sales"]), (1, 1))


class Unrealised(unittest.TestCase):
    def test_tax_if_sold_today_splits_lots_by_age_and_uses_the_exemption_left(self):
        lots = {"INE000A01010": [tg.Lot(D(100), D(100), date(2024, 1, 1)), tg.Lot(D(100), D(100), date(2026, 6, 1))]}
        u = tg.unrealised(lots, {"INE000A01010": D(250)}, NAMES, CATS, date(2026, 10, 12), D(125000))
        self.assertEqual((u["equity_long_term"], u["equity_short_term"]), (15000.0, 15000.0))
        self.assertAlmostEqual(u["estimated_tax_if_sold_today"], 15000 * 0.20, places=2)              # short-term only: long is under the exemption
        small = tg.unrealised(lots, {"INE000A01010": D(250)}, NAMES, CATS, date(2026, 10, 12), D(10000))
        self.assertAlmostEqual(small["estimated_tax_if_sold_today"], 15000 * 0.20 + 5000 * 0.125, places=2)


if __name__ == "__main__":
    unittest.main()
