"""A jointly owned home counts for each co-owner's own share only; an EPF balance you shared is rolled forward. Scratch database (TEST_DATABASE_URL)."""
import os
import unittest
from datetime import date
from decimal import Decimal as D
from unittest import mock

URL = os.getenv("TEST_DATABASE_URL")


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class Ownership(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.models import database
        from app.models.database import Base
        from tests.dbhelp import reset_schema
        cls.engine = create_engine(URL)
        reset_schema(cls.engine)
        Base.metadata.create_all(cls.engine)
        with mock.patch.object(database, "engine", cls.engine):
            from app.main import _migrate_db
            _migrate_db()
        cls.db = sessionmaker(bind=cls.engine)()
        from app.models.schemas import Owner
        cls.db.add_all([Owner(id="user", name="U"), Owner(id="wife", name="W")])
        cls.db.commit()

    def world(self, shares=(33, 34)):
        from app.models.schemas import ManualAsset, Owner
        db = self.db
        db.query(ManualAsset).delete()
        db.query(Owner).filter_by(id="user").update({"home_loan_share_pct": D(shares[0])})
        db.query(Owner).filter_by(id="wife").update({"home_loan_share_pct": D(shares[1])})
        db.flush()
        db.add_all([ManualAsset(owner_id="user", asset_type="real_estate", name="Flat", current_value=D(40_000_000)),
                    ManualAsset(owner_id="user", asset_type="liability", institution="Some Bank", name="Home Loan ***1", current_value=D(-10_000_000)),
                    ManualAsset(owner_id="user", asset_type="fd", name="FD", current_value=D(100_000), is_liquid=False)])
        db.commit()

    def nw(self, owner):
        from app.engine.networth import compute_for_owner
        return compute_for_owner(self.db, owner, date.today())

    def test_each_co_owner_counts_the_flat_and_its_loan_at_their_own_share(self):
        self.world()
        u, w = self.nw("user"), self.nw("wife")
        self.assertEqual(u["real_estate_value"], D(13_200_000)); self.assertEqual(w["real_estate_value"], D(13_600_000))
        self.assertEqual(u["total_liabilities"], D(3_300_000)); self.assertEqual(w["total_liabilities"], D(3_400_000))
        self.assertEqual(u["fd_value"], D(100_000))                                        # something that is not shared counts in full
        self.assertEqual(u["real_estate_value"] + w["real_estate_value"], D(26_800_000))    # 67%: the third belongs to a parent, nobody's here

    def test_with_no_shares_set_the_owner_of_the_asset_has_all_of_it(self):
        self.world(shares=(0, 0))
        u, w = self.nw("user"), self.nw("wife")
        self.assertEqual(u["real_estate_value"], D(40_000_000)); self.assertEqual(w["real_estate_value"], D(0))

    def test_an_epf_balance_is_rolled_forward_with_contributions_and_interest(self):
        from app.models.schemas import ManualAsset
        from app.engine import epf
        a = ManualAsset(owner_id="user", asset_type="epf", current_value=D(1_000_000), monthly_payment=D(70_000), source_statement_date=date(2026, 3, 31),
                        interest_rate=D("8.25"))
        self.assertEqual(epf.value_of(a, date(2026, 3, 31)), D(1_000_000))                 # the anchor itself
        later = float(epf.value_of(a, date(2026, 9, 30)))
        self.assertGreater(later, 1_000_000 + 6 * 70_000)                                  # six contributions plus interest
        self.assertLess(later, 1_000_000 + 6 * 70_000 + 1_000_000 * 0.0825)

    def test_a_balance_you_share_becomes_a_manual_asset_once_and_a_newer_one_replaces_it(self):
        from app.models.schemas import ManualAsset
        from app.engine import epf
        db = self.db
        db.query(ManualAsset).delete(); db.commit()
        e = {"owner": "user", "as_of": date(2026, 1, 31), "balance": 500_000.0, "employee_monthly": 30_000.0, "employer_monthly": 28_000.0}
        self.assertEqual(epf.sync(db, entries=[e]), 1)
        self.assertEqual(epf.sync(db, entries=[e]), 0)                                      # same anchor: nothing to do
        self.assertEqual(epf.sync(db, entries=[dict(e, as_of=date(2025, 1, 31), balance=1.0)]), 0)   # an older one is ignored
        self.assertEqual(epf.sync(db, entries=[dict(e, as_of=date(2026, 6, 30), balance=700_000.0)]), 1)
        a = db.query(ManualAsset).filter_by(asset_type="epf").one()
        self.assertEqual((a.current_value, a.monthly_payment, a.goal_tag), (D(700_000), D(58_000), "retirement"))
        self.assertEqual(epf.stale(db, today=date(2028, 1, 1)), ["user"])                   # over a year old

    def test_voluntary_contributions_grow_the_balance_but_the_plan_counts_only_the_committed_part(self):
        from app.models.schemas import ManualAsset
        from app.engine import epf
        db = self.db
        db.query(ManualAsset).delete(); db.commit()
        epf.sync(db, entries=[{"owner": "wife", "as_of": date(2025, 3, 31), "balance": 3_500_000.0, "employee_monthly": 13_000.0, "employer_monthly": 13_000.0, "voluntary_monthly": 8_000.0}])
        a = db.query(ManualAsset).filter_by(asset_type="epf").one()
        self.assertEqual(a.monthly_payment, D(34_000))                                      # everything that really goes in
        from app.planning import personal
        self.assertEqual(personal.epf_entries({"epf": [{"owner": "wife", "as_of": "2025-03-31", "balance": 1, "voluntary_monthly": 8000}]})[0]["voluntary_monthly"], 8000.0)


if __name__ == "__main__":
    unittest.main()
