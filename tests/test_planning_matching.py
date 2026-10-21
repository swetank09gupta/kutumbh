"""Matching holdings to goals (app/planning/matching.py): synthetic goals and holdings, no database."""
import unittest

from app.planning import matching as M


def goal(tag, years, need, held=None, kind=None, scope=("user", "wife"), tags=None):
    kind = kind or tag
    return M.Goal(tag, frozenset(tags or [tag]), kind, years, need, dict(held or {}), frozenset(scope), M.goal_mix(years, kind))


def item(key, value, weights, kind="mutual_fund", current=None, managed=True, owner="user"):
    return M.Item(key, owner, value, weights, kind, current, managed)


EQ, DEBT, GOLD = {"equity": 1.0}, {"debt": 1.0}, {"gold": 1.0}


class Matching(unittest.TestCase):
    def goals(self):
        return [goal("education", 13, 10_000_000), goal("retirement", 15, 20_000_000), goal("emergency", 0, 3_000_000, {"debt": 2_000_000})]

    def test_the_emergency_fund_never_takes_equity_and_a_debt_holding_closes_its_gap_first(self):
        out = M.assign([item("eq", 500_000, EQ), item("fd", 600_000, DEBT)], self.goals())
        self.assertNotEqual(out["eq"], "emergency")
        self.assertEqual(out["fd"], "emergency")                                    # its gap is 1,000,000: the debt holding goes there

    def test_once_the_gap_is_closed_further_debt_goes_to_the_long_goals(self):
        g = self.goals()
        out = M.assign([item("fd1", 900_000, DEBT), item("fd2", 800_000, DEBT)], g)
        self.assertEqual(out["fd1"], "emergency")
        self.assertNotEqual(out["fd2"], "emergency")                                # 100,000 left of the gap: not worth 800,000

    def test_a_new_holding_goes_to_the_emptiest_goal(self):
        g = [goal("education", 13, 10_000_000, {"equity": 4_000_000}), goal("retirement", 15, 10_000_000, {"equity": 500_000})]
        self.assertEqual(M.assign([item("new", 300_000, EQ)], g)["new"], "retirement")

    def test_a_holding_stays_while_its_goal_still_wants_it(self):
        g = [goal("education", 13, 10_000_000, {"equity": 1_000_000}), goal("retirement", 15, 10_000_000, {"equity": 1_200_000})]
        out = M.assign([item("a", 300_000, EQ, current="education"), item("b", 300_000, EQ, current="retirement")], g)
        self.assertEqual((out["a"], out["b"]), ("education", "retirement"))        # near-equal gaps: no churn

    def test_a_holding_moves_when_its_goal_is_full_in_that_asset(self):
        g = [goal("marriage", 22, 1_000_000, {"gold": 100_000}), goal("retirement", 15, 20_000_000)]
        out = M.assign([item("sgb", 400_000, GOLD, "bonds", current="marriage")], g)  # the goal's gold bucket (5% of 1,000,000) is overfull
        self.assertEqual(out["sgb"], "retirement")

    def test_tags_you_set_are_not_touched(self):
        out = M.assign([item("mine", 500_000, EQ, current="emergency", managed=False)], self.goals())
        self.assertNotIn("mine", out)

    def test_a_single_stock_needs_a_far_goal_and_ppf_is_not_for_emergencies(self):
        near = [goal("education", 6, 10_000_000), goal("emergency", 0, 3_000_000)]
        self.assertIsNone(M.assign([item("stock", 300_000, EQ, "equity")], near)["stock"])
        out = M.assign([item("ppf", 300_000, {"ppf": 1.0}, "manual")], [goal("emergency", 0, 3_000_000)])
        self.assertIsNone(out["ppf"])

    def test_a_goal_only_takes_holdings_of_people_in_its_scope(self):
        g = [goal("retirement", 15, 20_000_000, scope=("wife",)), goal("education", 13, 10_000_000, scope=("user", "wife"))]
        self.assertEqual(M.assign([item("his", 300_000, EQ, owner="user")], g)["his"], "education")

    def test_the_family_retirement_keeps_whichever_of_its_two_tags_a_holding_had(self):
        g = [goal("retirement", 15, 20_000_000, tags=["retirement", "wife_retirement"])]
        self.assertEqual(M.assign([item("e", 300_000, EQ, current="wife_retirement")], g)["e"], "wife_retirement")

    def test_a_second_run_changes_nothing(self):
        items = [item("a", 500_000, EQ), item("b", 400_000, GOLD, "bonds"), item("c", 300_000, DEBT), item("d", 200_000, EQ)]
        first = M.assign(items, self.goals())
        again = [item(i.key, i.value, i.weights, i.kind, first[i.key], True, i.owner) for i in items]
        self.assertEqual(M.assign(again, self.goals()), first)

    def test_a_goal_with_nothing_to_fund_attracts_nothing(self):
        out = M.assign([item("a", 100_000, EQ)], [goal("education", 13, 0)])
        self.assertIsNone(out["a"])


if __name__ == "__main__":
    unittest.main()
