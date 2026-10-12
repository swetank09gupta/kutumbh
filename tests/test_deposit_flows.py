import unittest
from datetime import date
from decimal import Decimal as D

from app.engine.deposit_flows import _fy_interest, ppf_measure


def rate(d):
    return 7.1


def year_close(opening, deposits, fy_end):
    """The balance the bank would print on 31 March: opening + deposits + the interest PPF's rule credits."""
    return opening + sum((a for _, a in deposits), D(0)) + _fy_interest(opening, deposits, fy_end, rate).quantize(D("0.01"))


class PpfMeasure(unittest.TestCase):
    def series(self):
        """FY24-25 fully on the bank statements; FY25-26 has a 1,50,000 deposit in July the bank rows do not show."""
        m24 = D("1271870")
        dep25 = [(date(2024, 5, 1), D("40000")), (date(2024, 5, 31), D("80000")), (date(2024, 11, 1), D("30000"))]
        m25 = year_close(m24, dep25, date(2025, 3, 31))
        hidden = [(date(2025, 7, 3), D("150000"))]
        listed26 = [(date(2025, 10, 2), D("25000"))]
        m26 = year_close(m25, listed26 + hidden, date(2026, 3, 31))
        snaps = [(date(2024, 3, 31), m24), (date(2025, 3, 31), m25), (date(2026, 3, 31), m26),
                 (date(2026, 4, 30), m26 + D("150000"))]                      # an April deposit after the last year-end
        return snaps, dep25 + listed26, (m24, m25, m26)

    def test_a_year_whose_deposits_are_all_on_bank_statements_reconciles_to_the_rupee(self):
        snaps, bank, _ = self.series()
        out = ppf_measure(snaps, bank, rate)
        self.assertEqual(out["years"][0]["status"], "reconciled")
        self.assertAlmostEqual(out["years"][0]["unexplained"], 0.0, places=0)

    def test_a_deposit_missing_from_the_bank_statements_is_found_dated_and_flagged(self):
        snaps, bank, _ = self.series()
        out = ppf_measure(snaps, bank, rate)
        y = out["years"][1]
        self.assertEqual(y["inferred"]["amount"], 150000.0)                       # a round amount, not a leftover
        self.assertEqual(y["inferred"]["month"], "Jul 2025")                      # the month, from the interest it earned
        self.assertEqual(y["inferred"]["confidence"], "high")
        self.assertIn("not on a bank statement", out["notes"][0])

    def test_measured_over_whole_years_the_return_is_the_notified_rate(self):
        from app.engine.xirr_engine import xirr
        snaps, bank, _ = self.series()
        out = ppf_measure(snaps, bank, rate)
        cfs = [(d, -float(a)) for d, a in out["flows"]] + [(out["terminal"][0], float(out["terminal"][1]))]
        self.assertAlmostEqual(xirr(cfs), 7.1, delta=0.15)

    def test_two_unlisted_deposits_are_not_passed_off_as_certain(self):
        # 60,000 and 20,000 in different months look like no single round deposit: say so, do not guess confidently
        m24 = D("1000000")
        deps = [(date(2024, 6, 30), D("60000")), (date(2024, 9, 4), D("20000"))]
        m25 = year_close(m24, deps, date(2025, 3, 31))
        out = ppf_measure([(date(2024, 3, 31), m24), (date(2025, 3, 31), m25)], [], rate)
        self.assertEqual(out["years"][0]["inferred"]["confidence"], "low")

    def test_an_unlabelled_transfer_that_closes_the_year_is_recognised_as_the_deposit(self):
        m24 = D("1000000")
        deps = [(date(2024, 6, 30), D("60000")), (date(2024, 9, 4), D("20000"))]
        m25 = year_close(m24, deps, date(2025, 3, 31))
        # the bank row for the 60,000 says only "INB MBS": it is offered as a candidate; the 20,000 is on no statement
        out = ppf_measure([(date(2024, 3, 31), m24), (date(2025, 3, 31), m25)], [], rate,
                          maybe=[(date(2024, 6, 30), D("60000")), (date(2024, 8, 14), D("33000"))])
        y = out["years"][0]
        self.assertEqual(y["found_among_transfers"], ["30 Jun 2024 ₹60,000"])         # the 33,000 does not fit: not taken
        self.assertEqual(y["inferred"]["amount"], 20000.0)

    def test_the_window_ends_at_the_last_31_march_not_at_a_later_deposit(self):
        snaps, bank, (_, _, m26) = self.series()
        out = ppf_measure(snaps, bank, rate)
        self.assertEqual(out["terminal"], (date(2026, 3, 31), m26))               # the April deposit is not in the return
        self.assertTrue(all(d <= date(2026, 3, 31) for d, _ in out["flows"]))

    def test_years_without_a_31_march_statement_are_not_measured(self):
        snaps = [(date(2021, 3, 31), D("600000")), (date(2022, 5, 31), D("969600")), (date(2024, 3, 31), D("1271870"))]
        self.assertIsNone(ppf_measure(snaps, [], rate))                           # no two consecutive year-ends

    def test_only_the_latest_run_of_consecutive_year_ends_is_used(self):
        a = D("1000000")
        b = year_close(a, [], date(2025, 3, 31))
        c = year_close(b, [], date(2026, 3, 31))
        snaps = [(date(2021, 3, 31), D("500000")), (date(2024, 3, 31), a), (date(2025, 3, 31), b), (date(2026, 3, 31), c)]
        out = ppf_measure(snaps, [], rate)
        self.assertEqual(out["flows"][0], (date(2024, 3, 31), a))                 # 2021 is cut off by the gap
        self.assertEqual(len(out["years"]), 2)

    def test_a_balance_lower_than_deposits_and_interest_explain_is_reported_not_hidden(self):
        a = D("1000000")
        snaps = [(date(2024, 3, 31), a), (date(2025, 3, 31), a)]                  # no interest at all: impossible
        out = ppf_measure(snaps, [], rate)
        self.assertIn("lower than", out["notes"][0])


from app.engine.deposit_flows import fd_flows


class FdFlows(unittest.TestCase):
    def snaps(self):
        # opened with 105,000; 13,000 broken in March; interest added to principal in Feb 2026; 55,000 broken in May 2026
        return [(date(2025, 2, 28), D("105000"), D("20")), (date(2025, 3, 31), D("92000"), D("562")),
                (date(2026, 1, 31), D("92000"), D("5934")), (date(2026, 2, 28), D("98417"), D("17")),
                (date(2026, 4, 30), D("98417"), D("1071")), (date(2026, 5, 31), D("43417"), D("708"))]

    def test_openings_withdrawals_and_capitalised_interest_are_told_apart(self):
        credits = [(date(2025, 3, 20), D("13050")), (date(2026, 5, 12), D("55200"))]       # break proceeds include interest
        flows = fd_flows(self.snaps(), date(2025, 2, 28), credits)
        self.assertEqual(flows, [(date(2025, 2, 28), D("-105000")), (date(2025, 3, 20), D("13050")),
                                 (date(2026, 5, 12), D("55200"))])                         # no flow for the interest added

    def test_a_withdrawal_with_no_matching_bank_credit_falls_mid_month(self):
        flows = fd_flows(self.snaps(), date(2025, 2, 28), [])
        self.assertEqual([a for _, a in flows], [D("-105000"), D("13000"), D("55000")])
        self.assertTrue(date(2025, 3, 1) < flows[1][0] < date(2025, 3, 31))

    def test_a_rise_that_is_not_the_accrued_interest_is_a_top_up(self):
        snaps = [(date(2025, 1, 31), D("50000"), D("200")), (date(2025, 2, 28), D("80000"), D("30"))]
        flows = fd_flows(snaps, date(2025, 1, 31), [])
        self.assertEqual([a for _, a in flows], [D("-50000"), D("-30000")])

    def test_history_that_starts_mid_life_opens_at_the_first_value(self):
        snaps = [(date(2026, 3, 31), D("40000"), D("875")), (date(2026, 4, 30), D("40000"), D("1091"))]
        flows = fd_flows(snaps, date(2025, 11, 30), [])
        self.assertEqual(flows, [(date(2026, 3, 31), D("-40875"))])


if __name__ == "__main__":
    unittest.main()
