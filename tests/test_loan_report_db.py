"""
The loan reconciled against the lender, on a scratch database, from the patterns found in the real HDFC letters:
  * the debited EMI lags a revised EMI by one instalment (Jul/Sep 2026) - the lender applies its letter EMI;
  * a rate reset lands mid-interval (Jan 2026 instalment) and fits to the rupee when the right month is chosen;
  * an instalment with no debit in the data;
  * 13 letters that all equal the repo in force + a constant spread.
Numbers here are synthetic. Needs a scratch Postgres (TEST_DATABASE_URL).
"""
import os
import unittest
import uuid
from datetime import date, timedelta
from decimal import Decimal as D
from unittest import mock

from app.engine import loan_schedule as sched

URL = os.getenv("TEST_DATABASE_URL")
TODAY = date(2026, 10, 12)


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class LoanWorld(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.models import database, schemas
        from app.models.database import Base
        from tests.dbhelp import reset_schema
        cls.engine = create_engine(URL)
        reset_schema(cls.engine)
        Base.metadata.create_all(cls.engine)
        with mock.patch.object(database, "engine", cls.engine):
            from app.main import _migrate_db
            _migrate_db()
        cls.S, cls.m = sessionmaker(bind=cls.engine), schemas

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        from sqlalchemy import text
        with self.engine.begin() as c:
            c.execute(text("TRUNCATE loan_anchors, transactions, bank_alerts, accounts, owners CASCADE"))
        self.db, m = self.S(), self.m
        db = self.db
        db.add(m.Owner(id="user", name="u", tax_regime="old", home_loan_share_pct=D(33)))
        db.add(m.Owner(id="wife", name="w", tax_regime="new", home_loan_share_pct=D(34)))
        db.flush()
        self.bank = m.Account(owner_id="user", institution="HDFC", account_type="bank", account_ref="1632")
        db.add(self.bank); db.commit()
        self.addCleanup(self.db.close)
        self.n = 0

    # ── builders ──
    def letter(self, as_on, after, prepaid=0, emi=170000, rate="7.1", left=None):
        self.n += 1
        self.db.add(self.m.LoanAnchor(owner_id="user", lender="HDFC", loan_no="1", as_on=as_on, prepaid=D(prepaid),
                                      principal_after=D(after), emi=D(emi), rate=D(rate), emis_left=left, natural_key=f"k{self.n}"))
        self.db.commit()

    def debit(self, d, amount=170000):
        self.n += 1
        self.db.add(self.m.Transaction(account_id=self.bank.id, date=d, amount=D(amount), transaction_type="debit",
                                       description="EMI", raw_description=f"ACH D- HDFC BANK LTD-{self.n}",
                                       fingerprint=uuid.uuid4().hex))
        self.db.commit()

    def next_letter_after(self, prev_after, emi, rates_by_instalment, prepaid):
        """The principal a lender following the monthly-rest rule would print: stepping the balance, then prepaying."""
        p = D(prev_after)
        for rate in rates_by_instalment:
            _i, _r, p = sched.step(p, rate, D(emi))
        return (p - D(prepaid)).quantize(D("1"))

    # ── the debited amount is not the EMI the lender applies ──
    def test_the_roll_applies_the_letter_emi_not_a_lagging_debit(self):
        from app.engine import loan
        self.letter(date(2026, 7, 1), 12803080, emi=164056, left=105)
        self.debit(date(2026, 7, 5), amount=170000)                  # stale mandate amount; the lender applied 164,056
        a = self.db.query(self.m.LoanAnchor).one()
        principal, last, rows = loan.roll(self.db, "user", a)
        _i, _r, expect = sched.step(D(12803080), 7.1, D(164056))
        self.assertEqual(principal, expect)
        self.assertEqual(rows[0]["debited"], D(170000))
        self.assertNotEqual(principal, sched.step(D(12803080), 7.1, D(170000))[2])        # mutation: using the debit would differ

    def test_a_lagging_debit_still_reconciles_and_is_flagged(self):
        from app.engine import loan_report
        self.letter(date(2026, 7, 1), 12803080, emi=164056, left=105)
        self.debit(date(2026, 7, 5), amount=170000)
        self.letter(date(2026, 8, 1), self.next_letter_after(12803080, 164056, [7.1], 500000), prepaid=500000, emi=164056)
        (r,) = loan_report.reconcile(self.db, "user")
        self.assertEqual(r["status"], "exact")
        self.assertTrue(r["debits"][0]["lag"])
        self.assertEqual(r["debits"][0]["debited"], 170000.0)

    # ── a letter the roll cannot reproduce is reported with its size ──
    def test_a_letter_that_does_not_fit_is_reported_not_smoothed(self):
        from app.engine import loan_report
        self.letter(date(2026, 8, 1), 12000000, left=90)
        self.debit(date(2026, 8, 5))
        self.letter(date(2026, 9, 1), self.next_letter_after(12000000, 170000, [7.1], 500000) + 10000, prepaid=500000)
        (r,) = loan_report.reconcile(self.db, "user")
        self.assertEqual(r["status"], "differs")
        self.assertAlmostEqual(abs(r["difference"]), 10000, delta=1)

    def test_a_missing_instalment_is_named_and_explains_the_gap_when_assumed_paid(self):
        from app.engine import loan_report
        self.letter(date(2026, 1, 1), 15000000)
        self.debit(date(2026, 1, 5)); self.debit(date(2026, 3, 5))                # February has no debit in the data
        self.letter(date(2026, 4, 1), self.next_letter_after(15000000, 170000, [7.1] * 3, 0), emi=170000)
        (r,) = loan_report.reconcile(self.db, "user")
        self.assertEqual(r["unseen_months"], ["2026-02"])
        self.assertEqual(r["status"], "exact_if_unseen_paid")

    # ── a rate reset in the middle of an interval ──
    def test_the_reset_month_is_found_when_the_rate_changes_inside_an_interval(self):
        from app.engine import loan_report
        self.letter(date(2025, 11, 1), 15737361, rate="7.35", left=138)
        for d in (date(2025, 11, 5), date(2025, 12, 5), date(2026, 1, 5), date(2026, 2, 5)):
            self.debit(d)
        after = self.next_letter_after(15737361, 170000, [7.35, 7.35, 7.10, 7.10], 310000)       # reset from the Jan instalment
        self.letter(date(2026, 3, 1), after, prepaid=310000, rate="7.10")
        (r,) = loan_report.reconcile(self.db, "user")
        self.assertEqual(r["status"], "exact")
        self.assertEqual(r["rate_change_from_instalment"], "2026-01")
        self.assertEqual(r["rate_change_date"], "2026-01-05")

    # ── the loan rate follows the repo; the forecast says so and is labelled ──
    def _rate_history_letters(self, last_rate="7.1"):
        for as_on, rate in ((date(2024, 12, 1), "8.35"), (date(2025, 6, 1), "7.85"), (date(2025, 11, 1), "7.35"),
                            (date(2026, 3, 1), "7.1"), (date(2026, 10, 1), last_rate)):
            self.letter(as_on, 12000000, rate=rate, left=100)

    def test_every_letter_equals_the_repo_in_force_plus_a_constant_spread(self):
        from app.engine import loan_report
        self._rate_history_letters()
        pt = loan_report.repo_pass_through(self.db, "user")
        self.assertTrue(pt["constant"])
        self.assertEqual(pt["spread"], 1.85)

    def test_one_letter_off_the_pattern_switches_the_forecast_off(self):
        from app.engine import loan_report
        self._rate_history_letters(last_rate="7.6")
        pt = loan_report.repo_pass_through(self.db, "user")
        self.assertFalse(pt["constant"]); self.assertIsNone(pt["spread"])

    def test_forecast_after_the_october_hike(self):
        from app.engine import loan_report
        self._rate_history_letters()
        self.debit(date(2026, 10, 5))
        f = loan_report.report(self.db, "user", TODAY)["forecast"]
        self.assertEqual((f["repo_now"], f["spread"], f["rate_now"], f["rate_expected"]), (5.5, 1.85, 7.1, 7.35))
        self.assertIn("forecast", f["label"])
        self.assertGreater(f["if_emi_unchanged"]["extra_interest"], 0)

    def test_no_forecast_when_the_rate_already_matches(self):
        from app.engine import loan_report
        for as_on, rate in ((date(2025, 6, 1), "7.85"), (date(2025, 11, 1), "7.35"), (date(2026, 3, 1), "7.1"), (date(2026, 10, 1), "7.35")):
            self.letter(as_on, 12000000, rate=rate, left=100)
        self.debit(date(2026, 10, 5))
        self.assertIsNone(loan_report.report(self.db, "user", TODAY)["forecast"])

    # ── payoff and what the first letter said ──
    def test_payoff_agrees_with_the_letters_emis_left_and_ends_where_the_schedule_says(self):
        from app.engine import loan_report
        self.letter(date(2026, 10, 1), 10619394, left=79)
        self.debit(date(2026, 10, 5))
        r = loan_report.report(self.db, "user", TODAY)
        self.assertEqual(r["emis_left_now"], 78)
        self.assertEqual(r["payoff"]["instalments"], 78)
        self.assertTrue(r["payoff"]["matches_letter"])
        self.assertEqual(r["payoff"]["ends"], "2033-04-05")
        self.assertEqual(r["next_emi"], "2026-11-05")

    # ── tax: shares, caps, regimes ──
    def test_tax_uses_each_owners_share_cap_and_regime(self):
        from app.engine import loan_report
        self.letter(date(2026, 3, 1), 15123710, rate="7.1", left=127)
        for d in (date(2026, 4, 5), date(2026, 5, 5), date(2026, 6, 5), date(2026, 7, 5), date(2026, 8, 5), date(2026, 9, 5), date(2026, 10, 5)):
            self.debit(d)
        t = loan_report.tax(self.db, "user", TODAY)
        by = {o["owner"]: o for o in t["owners"]}
        self.assertEqual(by["user"]["regime"], "old")
        self.assertEqual(by["user"]["claim_24b"], 200000.0)                          # capped
        self.assertGreater(by["user"]["interest_above_cap"], 0)
        self.assertTrue(by["user"]["uses_whole_80c"])
        self.assertEqual(by["wife"]["claim_24b"], 0.0)                               # new regime: nothing
        self.assertAlmostEqual(by["user"]["interest_share"] / by["wife"]["interest_share"], 33 / 34, places=6)
        fy = t["fy"]
        self.assertAlmostEqual(fy["interest_year"] + fy["principal_year"], 12 * 170000, delta=170000 * 0.01 + 6 * 1)   # a year of EMIs

    def test_financial_year_interest_uses_the_inferred_reset_month(self):
        from app.engine import loan_report
        self.letter(date(2025, 11, 1), 15737361, rate="7.35", left=138)
        for d in (date(2025, 11, 5), date(2025, 12, 5), date(2026, 1, 5), date(2026, 2, 5)):
            self.debit(d)
        rates = [7.35, 7.35, 7.10, 7.10]
        mar = self.next_letter_after(15737361, 170000, rates, 310000)
        self.letter(date(2026, 3, 1), mar, prepaid=310000, rate="7.10")
        self.debit(date(2026, 3, 5))
        p, expect_int, expect_prin = D(15737361), D(0), D(0)
        for rate in rates:                                                       # the lender's own steps, reset from January
            i, r, p = sched.step(p, rate, D(170000)); expect_int += i; expect_prin += r
        i, r, _ = sched.step(D(mar), 7.10, D(170000)); expect_int += i; expect_prin += r    # March, after the letter
        fy = loan_report.fy_split(self.db, "user", date(2026, 3, 20))
        self.assertEqual((fy["fy_start"], fy["fy_end"]), ("2025-04-01", "2026-03-31"))
        self.assertAlmostEqual(fy["interest_paid"], float(expect_int), delta=0.01)
        self.assertAlmostEqual(fy["principal_paid"], float(expect_prin), delta=0.01)
        self.assertEqual(fy["prepaid"], 310000.0)

    def test_the_eighty_c_advice_reads_the_schedule_not_its_own_estimate(self):
        import inspect
        from app.engine import recommendations
        src = inspect.getsource(recommendations)
        self.assertIn("loan_report.fy_split", src)
        self.assertNotIn("regular_principal_annual", src)

    # ── the consistency check ──
    def test_check_is_quiet_when_the_loan_reconciles(self):
        from app import consistency
        self.letter(date(2026, 8, 1), 12214776)
        self.debit(date(2026, 8, 5))
        self.letter(date(2026, 9, 1), self.next_letter_after(12214776, 170000, [7.1], 900000), prepaid=900000)
        for d in (date(2026, 9, 5), date.today()):
            self.debit(d)
        c = consistency.loan_reconciles(self.db)
        self.assertTrue(c["ok"], c["detail"])

    def test_check_warns_when_the_newest_letter_does_not_fit(self):
        from app import consistency
        self.letter(date(2026, 8, 1), 12214776)
        self.debit(date(2026, 8, 5))
        self.letter(date(2026, 9, 1), 11000000, prepaid=900000)                       # far from what the EMI explains
        self.debit(date.today())
        c = consistency.loan_reconciles(self.db)
        self.assertFalse(c["ok"]); self.assertIn("differs from the roll-forward", c["detail"])

    def test_check_warns_when_an_emi_is_overdue(self):
        from app import consistency
        self.letter(date(2026, 1, 1), 15000000)
        self.debit(date(2026, 1, 5))
        c = consistency.loan_reconciles(self.db)                                      # nothing since: next EMI was due 5 Feb
        self.assertFalse(c["ok"]); self.assertIn("no EMI debit seen", c["detail"])

    def test_check_is_registered(self):
        from app import consistency
        self.assertIn(consistency.loan_reconciles, consistency.CHECKS)


if __name__ == "__main__":
    unittest.main()
