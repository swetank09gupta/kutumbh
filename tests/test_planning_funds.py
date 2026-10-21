"""Fund profiles and the risk arithmetic behind matching (app/planning/funds.py, risk.py): synthetic returns, no network."""
import unittest
from dataclasses import replace

import numpy as np
import pandas as pd

from app.planning import funds, risk
from app.planning.assumptions import Assumptions

IDX = pd.date_range("2019-03-31", periods=90, freq="ME")
F = funds.FACTORS


def factors(seed=3):
    rng = np.random.default_rng(seed)
    base = rng.normal(0.008, 0.045, len(IDX))                     # a market everything shares, then each factor's own noise
    return pd.DataFrame({
        "large": base + rng.normal(0, 0.008, len(IDX)),
        "mid": base * 1.1 + rng.normal(0, 0.02, len(IDX)),
        "small": base * 1.2 + rng.normal(0, 0.03, len(IDX)),
        "gold": rng.normal(0.006, 0.04, len(IDX)),
        "debt": rng.normal(0.005, 0.008, len(IDX)),
    }, index=IDX)[list(F)]


NAVALL = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date
 
Open Ended Schemes(Equity Scheme - Large Cap Fund)
 
Some Mutual Fund
 
100001;INF000A01AA1;-;Some Large Cap Fund;Regular Plan;Growth Option;50.1;19-Oct-2026
100002;INF000A01AB9;INF000A01AC7;Some Large Cap Fund IDCW;Regular Plan;IDCW Option;40.1;19-Oct-2026
 
Close Ended Schemes(Other Scheme - Gold ETF)
 
Gold House
 
100003;INF000B01BB5;-;Some Gold ETF;Regular Plan;Growth Option;70.0;19-Oct-2026
"""


class Catalogue(unittest.TestCase):
    def test_every_isin_of_a_scheme_carries_its_sebi_category(self):
        c = funds.parse_catalog(NAVALL)
        self.assertEqual(c["INF000A01AA1"]["category"], "Equity Scheme - Large Cap Fund")
        self.assertEqual(c["INF000A01AC7"]["code"], 100002)                              # the reinvestment ISIN too
        self.assertEqual(c["INF000B01BB5"]["category"], "Other Scheme - Gold ETF")
        self.assertNotIn("-", c)


class Contents(unittest.TestCase):
    def test_style_analysis_recovers_the_mix_a_fund_was_built_from(self):
        f = factors()
        y = (0.7 * f["large"] + 0.2 * f["mid"] + 0.1 * f["debt"]).to_numpy() + np.random.default_rng(1).normal(0, 0.002, len(f))
        w = funds.simplex_ls(y, f.to_numpy())
        self.assertAlmostEqual(w.sum(), 1.0)
        self.assertTrue((w >= -1e-12).all())
        self.assertAlmostEqual(w[F.index("large")], 0.7, delta=0.1)
        self.assertAlmostEqual(w[F.index("debt")], 0.1, delta=0.06)

    def test_the_category_settles_what_correlated_indices_cannot(self):
        f = factors()
        y = (0.2 * f["large"] + 0.6 * f["mid"] + 0.2 * f["small"]).to_numpy() + np.random.default_rng(2).normal(0, 0.01, len(f))
        prior = funds.category_prior("Equity Scheme - Mid Cap Fund")
        free, pulled = funds.simplex_ls(y, f.to_numpy()), funds.simplex_ls(y, f.to_numpy(), prior)
        truth = np.array([0.2, 0.6, 0.2, 0, 0])
        self.assertLessEqual(np.abs(pulled - truth).sum(), np.abs(free - truth).sum() + 1e-9)

    def test_a_fund_with_history_gets_its_own_profile_one_without_gets_its_category(self):
        f = factors()
        y = (0.9 * f["large"] + 0.1 * f["debt"]).rename("y")
        p = funds.fund_profile("INF1", y, f, "Equity Scheme - Large Cap Fund")
        self.assertEqual(p.source, "style"); self.assertGreater(p.exposure[F.index("large")], 0.7); self.assertGreater(p.r2, 0.9)
        short = funds.fund_profile("INF2", y.head(10), f, "Equity Scheme - Mid Cap Fund")
        self.assertEqual(short.source, "sebi_category"); self.assertGreater(short.exposure[F.index("mid")], 0.5)
        unknown = funds.fund_profile("INF3", None, f, "Something New")
        self.assertEqual(unknown.source, "default")

    def test_a_poor_fit_is_not_trusted(self):
        f = factors()
        noise = pd.Series(np.random.default_rng(9).normal(0, 0.05, len(f)), index=f.index)
        self.assertEqual(funds.fund_profile("INF4", noise, f, "Equity Scheme - Flexi Cap Fund").source, "sebi_category")

    def test_categories_map_to_what_sebi_requires(self):
        p = funds.category_prior
        self.assertEqual(p("Equity Scheme - Small Cap Fund")[F.index("small")], 0.65)
        self.assertGreater(p("Equity Scheme - Large & Mid Cap Fund")[F.index("mid")], 0.4)          # not caught by the mid cap rule alone
        self.assertEqual(p("Other Scheme - Gold ETF")[F.index("gold")], 1.0)
        self.assertEqual(p("Debt Scheme - Liquid Fund")[F.index("debt")], 1.0)
        self.assertEqual(p("Other Scheme - Index Funds", "Nippon Nifty Smallcap 250 Index Fund")[F.index("small")], 0.65)
        self.assertIsNone(p("Nothing known"))

    def test_a_stock_is_its_beta_and_what_the_market_does_not_explain(self):
        f = factors()
        r = (1.3 * f["large"] + np.random.default_rng(4).normal(0, 0.03, len(f))).rename("s")
        p = funds.stock_profile("INE1", r, f, "Utilities")
        self.assertAlmostEqual(p.exposure[F.index("large")], 1.3, delta=0.3)
        self.assertAlmostEqual(np.sqrt(p.idio), 0.03 * np.sqrt(12), delta=0.04)
        self.assertEqual(funds.stock_profile("INE2", None, f).source, "default")

    def test_fixed_instruments(self):
        self.assertEqual(funds.fixed_profile("fd", "debt").exposure[F.index("debt")], 1.0)
        self.assertEqual(funds.fixed_profile("sgb", "gold").exposure[F.index("gold")], 1.0)
        self.assertEqual(funds.fixed_profile("fd", "debt").idio, 0.0)


class Risk(unittest.TestCase):
    def model(self):
        return risk.build(factors(), Assumptions())

    def test_the_horizon_is_the_one_in_twenty_years_formula(self):
        self.assertAlmostEqual(risk.horizon(0.09, 0.17), (1.645 * 0.17 / 0.09) ** 2)
        self.assertEqual(risk.horizon(0.05, 0.0), 0.0)
        self.assertEqual(risk.horizon(-0.01, 0.1), float("inf"))

    def test_riskier_needs_longer_and_a_better_return_needs_less(self):
        self.assertGreater(risk.horizon(0.09, 0.25), risk.horizon(0.09, 0.15))
        self.assertGreater(risk.horizon(0.06, 0.17), risk.horizon(0.10, 0.17))

    def test_the_safe_equity_share_rises_with_the_years_and_is_capped(self):
        m = self.model()
        shares = [risk.equity_share(m, y) for y in (0.25, 1, 3, 5, 10, 25)]
        self.assertEqual(shares, sorted(shares))
        self.assertEqual(shares[0], 0.0)
        self.assertEqual(shares[-1], risk.MAX_EQUITY)

    def test_diversifying_gold_needs_fewer_years_than_the_same_amount_of_equity(self):
        m = self.model()
        eq = np.array([1.0, 0, 0, 0, 0])
        mix = np.array([0.7, 0, 0, 0.3, 0])
        self.assertLess(risk.horizon(*[m.stats(mix, 0)[i] for i in (0, 1)]), risk.horizon(*[m.stats(eq, 0)[i] for i in (0, 1)]))

    def test_the_market_curve_replaces_the_table_when_set(self):
        from app.planning import allocation
        before = allocation.recommended_equity(4.0, "moderate", "retirement")
        allocation.set_market_curve(lambda y: 0.33)
        try:
            self.assertAlmostEqual(allocation.recommended_equity(4.0, "moderate", "retirement"), 0.33)
        finally:
            allocation.set_market_curve(None)
        self.assertEqual(allocation.recommended_equity(4.0, "moderate", "retirement"), before)


if __name__ == "__main__":
    unittest.main()
