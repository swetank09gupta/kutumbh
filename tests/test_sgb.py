"""SGB tranche facts and the coupon / subscription arithmetic, against amounts the banks actually credited."""
import unittest
from datetime import date
from decimal import Decimal as D

from app.knowledge import rules
from app.ledger import sgb, sgb_evidence as ev
from app.ledger.rules import compute_isin_state


class Registry(unittest.TestCase):
    def test_every_tranche_is_sourced_and_consistent(self):
        tenor = int(rules.value("sgb.tenor_years"))
        self.assertGreaterEqual(len(sgb.tranches()), 4)
        for t in sgb.tranches().values():
            self.assertRegex(t.source, r"^https?://")
            self.assertEqual(t.nominal - t.online, 50, t.series)                  # the online-application discount
            self.assertEqual(t.maturity_date, t.issue_date.replace(year=t.issue_date.year + tenor), t.series)
            self.assertLessEqual(t.verified_on, date.today())

    def test_the_wife_s_isin_is_the_june_2023_tranche_not_the_september_one(self):    # the shipped data error
        t = sgb.tranche("IN0020230069")
        self.assertEqual((t.series, t.issue_date, t.nominal), ("2023-24 Series I", date(2023, 6, 27), D(5926)))

    def test_an_unknown_tranche_is_unknown_not_guessed(self):
        self.assertIsNone(sgb.issue_date("IN0020990001", "GOI SGB 2031"))
        self.assertIsNone(sgb.issue_terms("IN0020990001"))


class CouponArithmetic(unittest.TestCase):
    def test_coupon_equals_the_credits_the_banks_actually_made(self):
        for isin, paid in (("IN0020210178", "1487.81"), ("IN0020220110", "1690.31"), ("IN0020220045", "1590.94"),
                           ("IN0020230069", "1851.87")):
            self.assertLessEqual(abs(sgb.tranche(isin).coupon(D(25)) - D(paid)), D("0.01"), isin)

    def test_coupon_dates_run_every_six_months_from_issue_and_stop_at_maturity(self):
        t = sgb.tranche("IN0020210178")
        d = ev.coupon_dates(t, date(2023, 6, 1))
        self.assertEqual(d, [date(2022, 5, 2), date(2022, 11, 2), date(2023, 5, 2)])
        self.assertEqual(ev.coupon_dates(t, date(2040, 1, 1))[-1], date(2029, 11, 2))

    def test_a_credit_is_a_coupon_only_if_both_amount_and_date_fit(self):
        today = date(2026, 10, 12)
        (hit,) = ev._match_coupon(D("1590.94"), date(2024, 6, 30), today)
        self.assertEqual((hit[0].isin, hit[1], hit[2]), ("IN0020220045", 25, date(2024, 6, 28)))
        self.assertEqual([h[0].isin for h in ev._match_coupon(D("1851.87"), date(2024, 12, 27), today)], ["IN0020230069"])
        self.assertEqual(ev._match_coupon(D("1590.94"), date(2024, 3, 15), today), [])      # right sum, wrong time of year
        self.assertEqual(ev._match_coupon(D("1591.94"), date(2024, 6, 30), today), [])      # right time, sum off by a rupee

    def test_coupons_stay_on_the_nominal_price_whatever_was_paid(self):
        t = sgb.tranche("IN0020220110")
        self.assertEqual(t.coupon(D(25)), D("1690.31"))                       # 25 x 5,409 x 1.25%, not 5,359
        self.assertEqual(sgb.issue_terms("IN0020220110"), (date(2022, 12, 27), D(5359)))

    def test_the_subscription_price_is_nominal_or_the_online_price(self):
        t = sgb.tranche("IN0020220110")
        self.assertEqual(t.nominal * 25 - t.online * 25, 1250)                               # what the Dec 2022 debit shows


class LedgerCost(unittest.TestCase):
    ISIN = "IN0020220110"

    def test_without_evidence_units_cost_the_online_price_and_say_so(self):
        s = compute_isin_state(self.ISIN, [], [], [(date(2026, 8, 31), D(25))])
        self.assertEqual(s.position.invested, D(25) * D(5359))                 # nominal 5,409 less Rs.50: what the real debit showed
        self.assertIn("issue_price_cost", s.position.flags)

    def test_a_subscription_debit_sets_cost_and_date_to_what_was_paid(self):
        s = compute_isin_state(self.ISIN, [], [], [(date(2026, 8, 31), D(25))], paid=(date(2022, 12, 19), D(5359), D(25)))
        self.assertEqual(s.position.invested, D(133975))
        self.assertEqual(s.position.first_buy_date, date(2022, 12, 19))
        self.assertIn("subscription_cost", s.position.flags)
        self.assertNotIn("issue_price_cost", s.position.flags)

    def test_units_beyond_the_subscription_stay_at_the_online_price(self):
        s = compute_isin_state(self.ISIN, [], [], [(date(2026, 8, 31), D(30))], paid=(date(2022, 12, 19), D(5359), D(25)))
        self.assertEqual(s.position.invested, D(30) * D(5359))
        self.assertEqual(s.position.quality, "exact")


if __name__ == "__main__":
    unittest.main()
