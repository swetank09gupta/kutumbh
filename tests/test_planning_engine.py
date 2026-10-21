"""
The planning engine (docs/PLANNING.md): proven against closed-form arithmetic with volatility switched off, then checked for the
properties a planner relies on (more saving never lowers the odds, the average is the forward assumption, runs repeat exactly).
No network and no database: history is synthetic.
"""
import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from unittest import mock

import numpy as np
import pandas as pd

from app.planning import history, simulate
from app.planning.assumptions import Assumptions

IDX = pd.date_range("2011-10-31", periods=181, freq="ME")


def data(vol=0.0, seed=1):
    rng = np.random.default_rng(seed)
    joint = pd.DataFrame({k: 0.005 + vol * rng.standard_normal(len(IDX)) for k in ("equity", "gold", "debt")}, index=IDX)
    return {"joint": joint, "equity_long": joint["equity"]}


FLAT = replace(Assumptions(), n_paths=50, inflation_sd=0.0, equity_vol_scale=1.0, spread_full_years=None)
DEBT_ONLY = {"debt": 1.0}


class ClosedForm(unittest.TestCase):
    def test_a_lump_sum_grows_at_the_after_tax_rate(self):
        out = simulate.simulate_accumulation(corpus0=100000, sip0=0, stepup=0, years=10, target_today=1, goal_inflation=0.05,
                                             start_weights=DEBT_ONLY, end_weights=DEBT_ONLY, glide_years=0, data=data(), a=FLAT)
        expect = 100000 * (1 + FLAT.after_tax("debt")) ** 10
        self.assertAlmostEqual(out["corpus_future"]["p50"], expect, delta=expect * 1e-9)
        self.assertEqual(out["probability"], 1.0)                                    # target of ₹1 today: reached

    def test_the_sip_for_confidence_is_exactly_the_sip_that_reaches_the_target(self):
        kw = dict(corpus0=200000, stepup=0.10, years=12, target_today=3_000_000, goal_inflation=0.06, start_weights=DEBT_ONLY,
                  end_weights=DEBT_ONLY, glide_years=0, data=data(), a=FLAT)
        need = simulate.simulate_accumulation(sip0=0, **kw)["sip_for_confidence"]["80"]
        self.assertGreater(need, 0)
        hit = simulate.simulate_accumulation(sip0=need, **kw)
        self.assertAlmostEqual(hit["corpus_future"]["p50"], hit["target_future"]["p50"], delta=1.0)
        self.assertEqual(simulate.simulate_accumulation(sip0=need * 1.01, **kw)["probability"], 1.0)
        self.assertEqual(simulate.simulate_accumulation(sip0=need * 0.99, **kw)["probability"], 0.0)

    def test_a_stepped_up_sip_matches_a_hand_loop(self):
        r = np.full((3, 36), 0.01)
        got = simulate.grow(r, 0.0, 1000.0, 0.10)[0]
        w = 0.0
        for m in range(36):
            w = (w + 1000 * 1.10 ** (m // 12)) * 1.01
        self.assertAlmostEqual(got, w, places=6)

    def test_retirement_corpus_needed_is_the_present_value_of_the_spending(self):
        a = FLAT                                                                      # basket inflation = general 5.5% (no own-spending drift)
        out = simulate.simulate_retirement(corpus0=0, sip0=0, stepup=0, years_to_retire=10, years_in_retirement=30, spend_today=1_200_000,
                                           start_weights=DEBT_ONLY, retire_weights=DEBT_ONLY, glide_years=0, data=data(), a=a)
        g, rm = (1 + a.inflation_general) ** (1 / 12), (1 + a.after_tax("debt", True)) ** (1 / 12)       # retired: the lower tax drag
        pv = sum(1_200_000 / 12 * g ** (120 + m) / rm ** m for m in range(360))
        self.assertAlmostEqual(out["required_corpus"]["90"], pv, delta=pv * 1e-9)
        self.assertEqual(out["probability"], 0.0)                                     # nothing saved: cannot last


class SpreadFade(unittest.TestCase):
    def test_a_premium_over_general_inflation_fades_after_ten_years_but_general_does_not(self):
        rng = lambda: np.random.default_rng(1)
        means = {"general": 0.05, "medical": 0.11}
        flat = simulate.inflation_paths(means, 0.0, 2, 480, rng())
        faded = simulate.inflation_paths(means, 0.0, 2, 480, rng(), 10, 0.25)
        self.assertTrue(np.allclose(flat["general"], faded["general"]))
        self.assertAlmostEqual(faded["medical"][0, 120], flat["medical"][0, 120], places=9)             # the first ten years are untouched
        self.assertLess(faded["medical"][0, 480], flat["medical"][0, 480] * 0.6)                     # a 40-year 11% run is ~65x; fading keeps it near 35x
        self.assertGreater(faded["medical"][0, 480], faded["general"][0, 480])                         # still dearer than general, never cheaper

    def test_the_fade_is_a_straight_line_to_the_floor(self):
        f = simulate.spread_fade(45, 10, 0.25, until=40)
        self.assertEqual(f[9], 1.0); self.assertAlmostEqual(f[25], 0.625); self.assertAlmostEqual(f[40], 0.25); self.assertAlmostEqual(f[44], 0.25)


class Properties(unittest.TestCase):
    def run_goal(self, sip, **over):
        a = replace(Assumptions(), n_paths=800)
        kw = dict(corpus0=500000, sip0=sip, stepup=0.05, years=15, target_today=5_000_000, goal_inflation=0.06,
                  start_weights={"equity": 0.7, "debt": 0.3}, end_weights={"equity": 0.2, "debt": 0.8}, glide_years=3,
                  data=data(0.04), a=a)
        kw.update(over)
        return simulate.simulate_accumulation(**kw)

    def test_more_saving_never_lowers_the_odds(self):
        p = [self.run_goal(s)["probability"] for s in (0, 10000, 25000, 60000, 150000)]
        self.assertEqual(p, sorted(p))
        self.assertLess(p[0], 0.2); self.assertGreater(p[-1], 0.8)

    def test_a_cautious_world_needs_more_saving_than_a_hopeful_one(self):
        lo = self.run_goal(0, a=replace(Assumptions(), equity=0.12, gold=0.08, debt=0.075, n_paths=800))["sip_for_confidence"]["80"]
        hi = self.run_goal(0, a=replace(Assumptions(), equity=0.085, gold=0.055, debt=0.065, n_paths=800))["sip_for_confidence"]["80"]
        self.assertGreater(hi, lo)

    def test_higher_confidence_costs_more(self):
        s = self.run_goal(0)["sip_for_confidence"]
        self.assertLess(s["50"], s["80"]); self.assertLess(s["80"], s["90"])

    def test_the_same_inputs_give_the_same_answer(self):
        self.assertEqual(self.run_goal(20000), self.run_goal(20000))

    def test_the_average_return_is_the_assumption_not_the_sample(self):
        # sample mean is 0.5%/month (6.2%/yr) with volatility; the equity path must compound at the 10.5% - 0.6% assumed
        a = replace(Assumptions(), n_paths=4000, equity_vol_scale=1.0)
        out = simulate.simulate_accumulation(corpus0=1.0, sip0=0, stepup=0, years=20, target_today=1, goal_inflation=0.0,
                                             start_weights={"equity": 1.0}, end_weights={"equity": 1.0}, glide_years=0, data=data(0.045), a=a)
        cagr = out["corpus_future"]["p50"] ** (1 / 20) - 1
        self.assertAlmostEqual(cagr, a.after_tax("equity"), delta=0.012)

    def test_glide_weights_move_to_the_end_mix_and_always_sum_to_one(self):
        w = simulate.glide_weights({"equity": 0.8, "debt": 0.2}, {"equity": 0.2, "debt": 0.5, "ppf": 0.3}, 120, 36)
        self.assertTrue(np.allclose(w.sum(axis=1), 1.0))
        self.assertTrue(np.allclose(w[0], [0.8, 0, 0.2, 0])); self.assertTrue(np.allclose(w[-1], [0.2, 0, 0.5, 0.3]))
        self.assertTrue(np.allclose(w[83], w[0]))                                      # flat until the glide starts


class History(unittest.TestCase):
    NAVS = {a: {f"2026-{m:02d}-{d:02d}": 100 + i for i, (m, d) in enumerate([(1, 15), (1, 31), (2, 28), (3, 31), (4, 30), (5, 29), (6, 30), (7, 31), (8, 31), (9, 30), (10, 5)])}
            for a in history.ASSETS}

    def test_the_month_in_progress_is_not_a_return(self):
        s = history.month_end_returns(self.NAVS["equity"], today=date(2026, 10, 21))
        self.assertEqual(s.index[-1], pd.Timestamp("2026-09-30"))

    def test_a_failed_fetch_uses_the_stale_cache_and_says_so(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"PLANNING_HISTORY_CACHE": f"{d}/h.json"}):
            open(f"{d}/h.json", "w").write(json.dumps({"fetched_on": "2026-01-01", "navs": self.NAVS}))
            with mock.patch.object(history, "fetch_nav", side_effect=OSError("offline")):
                out = history.load(today=date(2026, 10, 21))
            self.assertTrue(out["meta"]["stale"])

    def test_no_cache_and_no_network_is_an_error_not_a_guess(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"PLANNING_HISTORY_CACHE": f"{d}/none.json"}):
            with mock.patch.object(history, "fetch_nav", side_effect=OSError("offline")):
                with self.assertRaises(history.PlanningDataUnavailable):
                    history.load(today=date(2026, 10, 21))


if __name__ == "__main__":
    unittest.main()
