"""The printed running balance must chain day to day; a break points at a lost, merged or never-fetched transaction."""
import unittest
from datetime import date
from decimal import Decimal as D

from app.txn.continuity import day_breaks


def r(d, kind, amount, bal):
    return (date(2024, 11, d), kind, D(amount), D(bal))


class Chain(unittest.TestCase):
    def test_a_complete_account_has_no_breaks(self):
        rows = [r(22, "credit", "1015", "1252.26"), r(22, "debit", "523.49", "728.77"),
                r(24, "credit", "1016", "1744.77"), r(24, "debit", "576.81", "1167.96")]
        self.assertEqual(day_breaks(rows), [])

    def test_the_order_inside_a_day_does_not_matter(self):
        rows = [r(22, "credit", "1015", "1252.26"), r(22, "debit", "523.49", "728.77"),
                r(24, "debit", "576.81", "1167.96"), r(24, "credit", "1016", "1744.77")]       # listed the other way round
        self.assertEqual(day_breaks(rows), [])

    def test_a_merged_twin_shows_as_its_amount_and_the_next_day_picks_the_right_balance(self):
        # the statement has two 1,016 credits (1744.77 then 2760.77, then a 576.81 debit -> 2183.96); we kept one of them
        rows = [r(22, "debit", "523.49", "728.77"), r(24, "credit", "1016", "1744.77"), r(24, "debit", "576.81", "2183.96"),
                r(26, "debit", "5", "2178.96")]
        (b,) = day_breaks(rows)
        self.assertEqual((b["date"], b["missing"]), (date(2024, 11, 24), D("-1016.00")))          # a credit we do not hold

    def test_a_lost_debit_shows_as_a_positive_amount_and_the_chain_resumes(self):
        # the statement shows a 100 debit (628.77) and a 50 debit (578.77) on the 24th; we hold only the 50
        rows = [r(22, "debit", "523.49", "728.77"), r(24, "debit", "50", "578.77"), r(26, "credit", "10", "588.77"),
                r(27, "credit", "1", "589.77")]
        (b,) = day_breaks(rows)
        self.assertEqual((b["date"], b["missing"]), (date(2024, 11, 24), D("100.00")))

    def test_rows_without_a_balance_are_ignored(self):
        self.assertEqual(day_breaks([(date(2024, 11, 22), "debit", D(5), None)]), [])


if __name__ == "__main__":
    unittest.main()
