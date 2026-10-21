"""Matching holdings to goals (app/planning/matching.py): synthetic profiles and goals, no database, no network."""
import unittest

import numpy as np

from app.planning import funds, matching as M, risk
from app.planning.assumptions import Assumptions
from tests.test_planning_funds import factors

F = funds.FACTORS
MODEL = risk.build(factors(), Assumptions())


def prof(**w):
    e = np.zeros(len(F))
    for k, v in w.items():
        e[F.index(k)] = v
    return funds.Profile("p", e, 0.0, "style")


def goal(tag, years, need, value=0.0, kind=None, scope=("user", "wife"), tags=None):
    g = M.Goal(tag, frozenset(tags or [tag]), kind or tag, years, need, frozenset(scope))
    g.value = value
    g.exposure = value * np.eye(len(F))[F.index("debt")]              # what is already held is cash-like
    return g


def item(key, value, profile, kind="mutual_fund", current=None, managed=True, owner="user"):
    return M.Item(key, owner, value, profile, kind, current, managed)


EQ, DEBT, GOLD = prof(large=1.0), prof(debt=1.0), prof(gold=1.0)
SMALL = prof(small=1.0)


class Matching(unittest.TestCase):
    def goals(self):
        return [goal("education", 13, 10_000_000), goal("retirement", 15, 20_000_000)]

    def test_nothing_is_matched_to_the_emergency_fund_and_a_stale_emergency_tag_is_released(self):
        out = M.assign(MODEL, [item("eq", 500_000, EQ), item("old", 600_000, DEBT, current="emergency")], self.goals())
        self.assertNotIn("emergency", out.values())
        self.assertIn(out["old"], ("education", "retirement"))

    def test_a_goal_is_not_given_more_risk_than_its_years_allow(self):
        near, far = goal("education", 3, 10_000_000), goal("retirement", 20, 10_000_000)
        out = M.assign(MODEL, [item("small", 1_000_000, SMALL)], [near, far])
        self.assertEqual(out["small"], "retirement")                                # small caps need over a decade: the 3-year goal cannot hold them

    def test_gold_diversifies_so_a_goal_near_its_budget_still_takes_it(self):
        g = goal("education", 7, 10_000_000)
        g.value, g.exposure = 2_000_000, 2_000_000 * np.array([1.0, 0, 0, 0, 0, 0])      # already all large-cap
        base = M.portfolio_horizon(MODEL, g)
        with_gold = M.portfolio_horizon(MODEL, g, item("g", 1_000_000, GOLD))
        self.assertLess(with_gold, base)

    def test_a_new_holding_goes_to_the_goal_furthest_from_funded(self):
        a, b = goal("education", 13, 10_000_000, 4_000_000), goal("retirement", 15, 10_000_000, 500_000)
        self.assertEqual(M.assign(MODEL, [item("new", 300_000, EQ)], [a, b])["new"], "retirement")

    def test_a_holding_stays_while_its_goal_still_wants_it(self):
        a, b = goal("education", 13, 10_000_000, 1_000_000), goal("retirement", 15, 10_000_000, 1_200_000)
        out = M.assign(MODEL, [item("a", 300_000, EQ, current="education"), item("b", 300_000, EQ, current="retirement")], [a, b])
        self.assertEqual((out["a"], out["b"]), ("education", "retirement"))

    def test_a_holding_moves_when_its_goal_could_never_hold_it(self):
        a, b = goal("education", 2, 10_000_000), goal("retirement", 20, 10_000_000)
        out = M.assign(MODEL, [item("small", 400_000, SMALL, current="education")], [a, b])
        self.assertEqual(out["small"], "retirement")

    def test_tags_you_set_are_not_touched(self):
        self.assertNotIn("mine", M.assign(MODEL, [item("mine", 500_000, EQ, current="education", managed=False)], self.goals()))

    def test_ppf_is_locked_so_not_for_short_goals(self):
        out = M.assign(MODEL, [item("ppf", 300_000, DEBT, "ppf")], [goal("education", 3, 5_000_000)])
        self.assertIsNone(out["ppf"])

    def test_a_goal_only_takes_holdings_of_people_in_its_scope(self):
        g = [goal("retirement", 15, 20_000_000, scope=("wife",)), goal("education", 13, 10_000_000)]
        self.assertEqual(M.assign(MODEL, [item("his", 300_000, EQ, owner="user")], g)["his"], "education")

    def test_a_second_run_changes_nothing(self):
        items = [item("a", 500_000, EQ), item("b", 400_000, GOLD, "bonds"), item("c", 300_000, DEBT), item("d", 200_000, SMALL)]
        first = M.assign(MODEL, items, self.goals())
        again = [item(i.key, i.value, i.profile, i.kind, first[i.key], True, i.owner) for i in items]
        self.assertEqual(M.assign(MODEL, again, self.goals()), first)

    def test_a_goal_with_nothing_to_fund_attracts_nothing(self):
        self.assertIsNone(M.assign(MODEL, [item("a", 100_000, EQ)], [goal("education", 13, 0)])["a"])


if __name__ == "__main__":
    unittest.main()
