"""Deadlines: the date rules (pure), then the engine against a scratch household with a fixed 'today'."""
import os
import unittest
import uuid
from datetime import date
from decimal import Decimal as D
from unittest import mock

from app.engine import deadlines as dl

URL = os.getenv("TEST_DATABASE_URL")
TODAY = date(2026, 10, 12)


class DateRules(unittest.TestCase):
    def test_financial_year_edges(self):
        self.assertEqual(dl.fy_bounds(date(2027, 3, 31)), (date(2026, 4, 1), date(2027, 3, 31)))
        self.assertEqual(dl.fy_bounds(date(2027, 4, 1)), (date(2027, 4, 1), date(2028, 3, 31)))
        self.assertEqual(dl.fy_label(date(2026, 4, 1)), "FY2026-27")

    def test_next_ppf_cutoff(self):
        self.assertEqual(dl.next_cutoff(date(2026, 10, 4), 5), date(2026, 10, 5))
        self.assertEqual(dl.next_cutoff(date(2026, 10, 5), 5), date(2026, 10, 5))      # the 5th itself still counts
        self.assertEqual(dl.next_cutoff(date(2026, 10, 6), 5), date(2026, 11, 5))
        self.assertEqual(dl.next_cutoff(date(2026, 12, 20), 5), date(2027, 1, 5))

    def test_ppf_matures_15_fys_after_the_year_of_opening(self):
        self.assertEqual(dl.ppf_maturity(date(2014, 10, 31), 15), date(2030, 3, 31))
        self.assertEqual(dl.ppf_maturity(date(2015, 3, 31), 15), date(2030, 3, 31))    # last day of FY2014-15
        self.assertEqual(dl.ppf_maturity(date(2015, 4, 1), 15), date(2031, 3, 31))     # first day of the next FY

    def test_add_years_handles_leap_day(self):
        self.assertEqual(dl.add_years(date(2024, 2, 29), 3), date(2027, 2, 28))

    def test_elss_sales_consume_the_oldest_units_first(self):
        t = [(date(2023, 1, 1), "B", D(10), D(1000)), (date(2024, 1, 1), "B", D(10), D(1200)),
             (date(2024, 6, 1), "S", D(15), D(0))]
        out = dl.elss_unlocks(t, 3)
        self.assertEqual(out, [(date(2027, 1, 1), D(5), D(600))])                      # only half of the 2024 lot is left

    def test_elss_unlock_is_three_years_after_each_instalment(self):
        out = dl.elss_unlocks([(date(2024, 2, 21), "B", D(3), D(300))], 3)
        self.assertEqual(out[0][0], date(2027, 2, 21))


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class Engine(unittest.TestCase):
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
        cls.m, cls.db = schemas, sessionmaker(bind=cls.engine)()
        cls._world()

    @classmethod
    def tearDownClass(cls):
        cls.db.rollback(); cls.db.close(); cls.engine.dispose()

    @classmethod
    def _world(cls):
        m, db = cls.m, cls.db
        for oid in ("user", "wife"):
            db.add(m.Owner(id=oid, name=oid))
        db.flush()
        zer = m.Account(owner_id="user", institution="Zerodha", account_type="stocks", account_ref="z")
        db.add(zer); db.flush()

        def asset(owner, typ, name, value, **kw):
            a = m.ManualAsset(owner_id=owner, asset_type=typ, name=name, institution="SBI", current_value=D(value), **kw)
            db.add(a); db.flush(); return a

        p_user = asset("user", "ppf", "Union Bank PPF", 100000)                       # opening date unknown
        p_wife = asset("wife", "ppf", "SBI PPF", 500000)
        db.add(m.ManualAssetContribution(asset_id=p_user.id, date=date(2026, 5, 3), amount=D(50000)))
        db.add(m.ManualAssetContribution(asset_id=p_user.id, date=date(2026, 3, 30), amount=D(150000)))   # previous FY
        db.add(m.DepositSnapshot(owner_id="wife", name="SBI PPF", asset_type="ppf", as_on=date(2026, 8, 31),
                                 principal=D(500000), open_date=date(2014, 10, 31)))
        # deposits: live, matured but still listed, closed since (not in newest statement), typed by hand
        for name, mat in (("SBI FD ...1111", date(2027, 1, 20)), ("SBI FD ...2222", date(2026, 10, 1)),
                          ("SBI FD ...3333", date(2027, 2, 1))):
            asset("user", "fd", name, 100000, invested_amount=D(100000), maturity_date=mat)
        asset("user", "fd", "SBI FD ...4444", 50000, invested_amount=D(50000), maturity_date=date(2027, 1, 5))   # hand-entered
        asset("user", "fd", "SBI FD ...9999", 10000, invested_amount=D(10000), maturity_date=date(2026, 12, 1))  # inside the window
        asset("user", "fd", "SBI FD ...7777", 10000, invested_amount=D(10000), maturity_date=date(2029, 1, 1))   # beyond horizon
        for name in ("SBI FD ...1111", "SBI FD ...2222", "SBI FD ...3333", "SBI FD ...9999", "SBI FD ...7777"):
            db.add(m.DepositSnapshot(owner_id="user", name=name, asset_type="fd", as_on=date(2026, 9, 30), principal=D(100000)))
        db.add(m.DepositSnapshot(owner_id="user", name="SBI FD ...3333", asset_type="fd", as_on=date(2026, 8, 31), principal=D(100000)))
        db.add(m.DepositSnapshot(owner_id="user", name="SBI FD ...8888", asset_type="fd", as_on=date(2026, 8, 31), principal=D(1)))
        asset("user", "fd", "SBI FD ...8888", 5000, invested_amount=D(5000), maturity_date=date(2026, 12, 25))   # absent from newest → closed
        # ELSS: two instalments three years before, one that is already free, one sold
        def trade(d, side, q, amt, isin="INF9ELSS0001", name="ACME ELSS TAX SAVER"):
            db.add(m.InstrumentTrade(owner_id="user", account_id=zer.id, isin=isin, instrument_name=name, holding_type="mutual_fund",
                                     trade_date=d, side=side, quantity=D(q), amount=D(amt), source="coin", natural_key=uuid.uuid4().hex))
        trade(date(2023, 6, 1), "B", 10, 1000)                  # free since 1 Jun 2026
        trade(date(2024, 1, 10), "B", 10, 1500)                 # 10 Jan 2027
        trade(date(2024, 1, 25), "B", 10, 1500)                 # 25 Jan 2027  (same month: one line)
        trade(date(2024, 3, 3), "B", 10, 1500, isin="INF9PLAIN001", name="ACME BLUECHIP FUND")   # not ELSS
        trade(date(2026, 6, 1), "S", 10, 1200)                  # sells the oldest (free) lot
        # SGB held by the wife (2023-24 Series I, matures Jun 2031) and by the user (2021-22 Series VII, Nov 2029)
        for owner, isin, nm in (("wife", "IN0020230069", "SGB 2031"), ("user", "IN0020210178", "SGB 2021-22 SERIES VII")):
            acc = m.Account(owner_id=owner, institution="Zerodha", account_type="stocks", account_ref=f"s{owner}")
            db.add(acc); db.flush()
            db.add(m.Holding(account_id=acc.id, snapshot_date=TODAY, isin=isin, instrument_name=nm, holding_type="bonds",
                             current_value=D(150000), units=D(25), source_file="ledger:rollforward"))
            db.add(m.StatementBalance(owner_id=owner, family="cdsl", as_on=date(2026, 9, 30), isin=isin, qty=D(25)))
        db.add(m.LoanAnchor(owner_id="user", lender="HDFC", loan_no="1", as_on=date(2026, 10, 1), prepaid=D(0),
                            principal_after=D(10000000), emi=D(170000), rate=D("7.1"), natural_key="a1"))
        db.commit()

    def build(self, **kw):
        return dl.build(self.db, today=TODAY, **kw)

    def items(self, kind, **kw):
        return [i for i in self.build(**kw)["items"] if i["kind"] == kind]

    def test_ppf_headroom_counts_only_this_financial_year(self):
        (u,) = [i for i in self.items("ppf_deposit") if i["owner"] == "user"]
        self.assertEqual(u["date"], "2026-11-05")
        self.assertIn("₹50,000 of ₹150,000", u["detail"])          # the 150,000 paid in March 2026 belongs to the last FY
        self.assertIn("₹100,000 still open", u["detail"])

    def test_ppf_maturity_only_with_a_known_opening_date(self):
        out = self.build(horizon_days=1830)
        self.assertEqual([i["date"] for i in out["items"] if i["kind"] == "ppf_maturity"], ["2030-03-31"])
        self.assertEqual(len(out["gaps"]), 1)
        self.assertIn("Union Bank PPF", out["gaps"][0]["what"])

    def test_ppf_year_end_item_is_the_31st_of_march(self):
        self.assertEqual({i["date"] for i in self.items("ppf_fy_end")}, {"2027-03-31"})

    def test_deposits_listed_are_live_and_inside_the_window(self):
        names = {i["title"].split(" matur")[0] for i in self.items("fd_maturity")}
        self.assertEqual(names, {"SBI FD ...2222", "SBI FD ...1111", "SBI FD ...3333", "SBI FD ...4444", "SBI FD ...9999"})
        late = [i for i in self.items("fd_maturity") if "2222" in i["title"]][0]
        self.assertEqual(late["status"], "past")
        self.assertIn("still lists it", late["detail"])

    def test_a_closed_deposit_is_not_shown(self):
        self.assertFalse([i for i in self.items("fd_maturity") if "8888" in i["title"]])

    def test_elss_groups_by_month_and_skips_what_is_free_or_sold(self):
        out = self.items("elss_unlock")
        self.assertEqual(len(out), 1)                               # Jan 2027 only; the non-ELSS fund is ignored
        self.assertEqual(out[0]["date"], "2027-01-10")
        self.assertIn("20.000 units", out[0]["detail"])
        self.assertIn("₹3,000", out[0]["detail"])

    def test_sgb_shows_maturity_not_early_exit(self):
        self.assertEqual(self.items("sgb_maturity"), [])             # 2029+ is outside a year
        out = self.items("sgb_maturity", horizon_days=1830)
        self.assertEqual({(i["owner"], i["date"]) for i in out}, {("user", "2029-11-02"), ("wife", "2031-06-27")})
        self.assertFalse([i for i in self.build(horizon_days=1830)["items"] if "exit" in i["title"].lower() or "redeem early" in i["detail"].lower()])

    def test_sgb_coupons_are_dated_from_the_issue_date_with_the_expected_amount(self):
        out = {(i["owner"], i["date"]): i for i in self.items("sgb_coupon")}
        self.assertEqual(set(out), {("user", "2026-11-02"), ("wife", "2026-12-27")})        # next coupon of each tranche
        self.assertIn("₹1,488", out[("user", "2026-11-02")]["detail"])                      # 25 g x 4,761 x 1.25%
        self.assertIn("₹1,852", out[("wife", "2026-12-27")]["detail"])                      # 25 g x 5,926 x 1.25%
        self.assertEqual(out[("wife", "2026-12-27")]["rules"], ["sgb.coupon_rate"])

    def test_repo_change_is_flagged_against_the_loan_letter(self):
        (i,) = self.items("loan_repo_change")
        self.assertEqual(i["date"], "2026-10-07")
        self.assertIn("(7.1% in the lender", i["detail"])
        self.assertEqual(i["rules"], ["rbi.repo_rate"])

    def test_every_item_cites_a_resolvable_rule_or_evidence(self):
        payload = self.build(horizon_days=1830)
        self.assertGreater(len(payload["items"]), 10)
        self.assertEqual(dl.uncited(payload, TODAY), [])

    def test_the_guard_catches_an_item_without_a_basis(self):                      # mutation
        payload = self.build()
        payload["items"].append({"title": "orphan", "rules": [], "evidence": ""})
        payload["items"].append({"title": "stale rule", "rules": ["no.such.rule"], "evidence": "x"})
        self.assertEqual({i["title"] for i in dl.uncited(payload, TODAY)}, {"orphan", "stale rule"})

    def test_items_are_in_date_order(self):
        d = [i["date"] for i in self.build()["items"]]
        self.assertEqual(d, sorted(d))

    def test_consistency_check_is_registered_and_passes(self):
        from app import consistency
        self.assertIn(consistency.deadlines_have_a_basis, consistency.CHECKS)
        self.assertTrue(consistency.deadlines_have_a_basis(self.db)["ok"])


if __name__ == "__main__":
    unittest.main()
