"""A payout deposit takes the bank's own balance from a newer alert; a newer statement, a cumulative deposit and an older alert do not. Scratch database."""
import os
import unittest
from datetime import date
from decimal import Decimal as D
from unittest import mock

URL = os.getenv("TEST_DATABASE_URL")


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class Align(unittest.TestCase):
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
        cls.db.add(Owner(id="user", name="U")); cls.db.commit()

    def world(self, stmt_date=date(2026, 8, 31), notes=None, maturity=None):
        from app.models.schemas import BankAlertRow, ManualAsset
        db = self.db
        db.query(BankAlertRow).delete(); db.query(ManualAsset).delete()
        db.add(ManualAsset(owner_id="user", asset_type="fd", institution="SBI", name="SBI FD ...6925", current_value=D(140000), accrued_interest=D(483),
                           source_statement_date=stmt_date, notes=notes, maturity_date=maturity))
        for i, d in enumerate((date(2026, 9, 11), date(2026, 10, 11))):
            db.add(BankAlertRow(owner_id="user", bank="SBI", account_last4="6925", direction="debit", amount=D(652), txn_date=d, kind="transfer",
                                balance_after=D(139970), status="untracked", natural_key=f"k{i}{stmt_date}{notes}"))
        db.commit()

    def run_align(self):
        from app.txn.alert_recon import align_deposits_to_alerts
        return align_deposits_to_alerts(self.db)

    def value(self):
        from app.models.schemas import ManualAsset
        self.db.expire_all()
        return self.db.query(ManualAsset).one()

    def test_a_newer_alert_balance_becomes_the_principal_and_the_accrued_interest_stays(self):
        self.world()
        self.assertEqual(len(self.run_align()), 1)
        a = self.value()
        self.assertEqual((a.current_value, a.accrued_interest, a.source_statement_date), (D(139970), D(483), date(2026, 10, 11)))
        self.assertIn("statement had ₹140,000", a.notes)
        self.assertEqual(self.run_align(), [])                                           # once: nothing left to change

    def test_a_statement_newer_than_the_alert_wins(self):
        self.world(stmt_date=date(2026, 10, 31))
        self.assertEqual(self.run_align(), [])
        self.assertEqual(self.value().current_value, D(140000))

    def test_a_deposit_that_compounds_is_left_to_its_contract(self):
        self.world(notes="Maturity value: ₹170,000", maturity=date(2030, 1, 1))
        self.assertEqual(self.run_align(), [])
        self.assertEqual(self.value().current_value, D(140000))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class Renewal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Align.engine
        cls.db = Align.db

    def world(self, credit=None, stmt_date=date(2026, 9, 30)):
        from app.models.schemas import BankAlertRow, ManualAsset
        db = self.db
        db.query(BankAlertRow).delete(); db.query(ManualAsset).delete()
        db.add(ManualAsset(owner_id="user", asset_type="fd", institution="SBI", name="SBI FD ...6925", current_value=D(139970), start_date=date(2024, 10, 11),
                           maturity_date=date(2026, 10, 11), source_statement_date=stmt_date))
        if credit:
            db.add(BankAlertRow(owner_id="user", bank="SBI", account_last4="3316", direction="credit", amount=D(credit), txn_date=date(2026, 10, 12), kind="transfer",
                                status="pending", natural_key=f"c{credit}"))
        db.commit()

    def run_renew(self):
        from app.txn.alert_recon import renew_matured_deposits
        out = renew_matured_deposits(self.db, today=date(2026, 10, 24))
        from app.models.schemas import ManualAsset
        self.db.expire_all()
        return out, self.db.query(ManualAsset).one()

    def test_a_matured_deposit_with_no_payout_is_renewed_one_year_on_and_says_so(self):
        self.world()
        out, a = self.run_renew()
        self.assertEqual(len(out), 1)
        self.assertEqual(a.maturity_date, date(2027, 10, 11))
        self.assertIn("Auto-renewed", a.notes)
        self.assertEqual(self.run_renew()[0], [])                                          # a second run changes nothing

    def test_a_credit_of_its_size_into_savings_means_it_was_paid_out_and_stays_matured(self):
        self.world(credit=141000)
        out, a = self.run_renew()
        self.assertEqual((out, a.maturity_date), ([], date(2026, 10, 11)))

    def test_a_statement_dated_after_the_maturity_is_left_alone(self):
        self.world(stmt_date=date(2026, 10, 31))
        self.assertEqual(self.run_renew()[0], [])

