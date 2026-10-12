import unittest
from datetime import date
from decimal import Decimal as D

from app.engine.deposit_flows import ppf_flows

FLAT = staticmethod(lambda d: 7.1)


def rate(d):
    return 7.1


class PpfFlows(unittest.TestCase):
    def test_a_bank_transfer_is_the_contribution_and_explains_the_balance_step(self):
        snaps = [(date(2025, 4, 30), D("500000")), (date(2025, 5, 31), D("600000"))]
        flows, notes = ppf_flows(snaps, [(date(2025, 5, 10), D("100000"))], rate)
        self.assertEqual(flows, [(date(2025, 4, 30), D("500000")), (date(2025, 5, 10), D("100000"))])
        self.assertEqual(notes, [])

    def test_interest_credited_on_31_march_is_not_a_contribution(self):
        # nothing paid in; the balance rises by a year of 7.1% on 31 March
        snaps = [(date(2023, 3, 31), D("1000000")), (date(2024, 2, 29), D("1000000")), (date(2024, 3, 31), D("1071000"))]
        flows, _ = ppf_flows(snaps, [], rate)
        self.assertEqual(len(flows), 1)                                     # only the opening position: no deposit invented

    def test_a_rise_with_no_bank_transfer_is_an_undated_deposit_placed_mid_gap(self):
        snaps = [(date(2022, 7, 31), D("969600")), (date(2022, 8, 31), D("1069600"))]
        flows, notes = ppf_flows(snaps, [], rate)
        self.assertEqual(flows[1][1], D("100000.00"))
        self.assertTrue(date(2022, 7, 31) < flows[1][0] < date(2022, 8, 31))
        self.assertIn("no bank statement", notes[0])

    def test_the_rebuilt_flows_reproduce_the_notified_rate(self):
        from app.engine.xirr_engine import xirr
        snaps = [(date(2023, 3, 31), D("1000000"))]
        bal, d = D("1000000"), date(2023, 3, 31)
        dates = [date(2023, 4, 30), date(2023, 5, 31), date(2023, 6, 30), date(2023, 7, 31), date(2023, 8, 31), date(2023, 9, 30),
                 date(2023, 10, 31), date(2023, 11, 30), date(2023, 12, 31), date(2024, 1, 31), date(2024, 2, 29),
                 date(2024, 3, 31)]
        for x in dates:
            snaps.append((x, bal if x < date(2024, 3, 31) else bal * D("1.071")))
        flows, _ = ppf_flows(snaps, [], rate)
        cfs = [(d, -float(a)) for d, a in flows] + [(snaps[-1][0], float(snaps[-1][1]))]
        self.assertAlmostEqual(xirr(cfs), 7.1, delta=0.4)

    def test_a_part_year_before_the_first_year_end_is_not_counted(self):
        snaps = [(date(2020, 10, 31), D("578766")), (date(2021, 2, 28), D("578766")),
                 (date(2021, 3, 31), D("619796")), (date(2021, 4, 30), D("619796"))]
        flows, _ = ppf_flows(snaps, [], rate)
        self.assertEqual(flows[0], (date(2021, 3, 31), D("619796")))       # measured from the first 31 March

    def test_one_statement_is_no_history(self):
        flows, notes = ppf_flows([(date(2025, 1, 31), D("1000"))], [], rate)
        self.assertEqual(flows, [])


if __name__ == "__main__":
    unittest.main()


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
