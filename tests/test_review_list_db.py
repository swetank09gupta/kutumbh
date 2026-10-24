"""The Review list says when a payee last happened, for how much and from which account (and the time, when a bank alert confirmed it). Scratch database."""
import os
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal as D
from unittest import mock

URL = os.getenv("TEST_DATABASE_URL")


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class ReviewList(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.models import database, schemas  # noqa: F401
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
        cls.db.add_all([Owner(id="user", name="U"), Owner(id="wife", name="W")]); cls.db.commit()

    def test_each_payee_names_its_latest_payment_and_the_accounts_it_came_from(self):
        from app.models.schemas import Account, BankAlertRow, Transaction
        from app.txn.pipeline import review_list
        db = self.db
        hdfc = Account(owner_id="user", institution="HDFC", account_type="bank", account_ref="1632")
        sbi = Account(owner_id="wife", institution="SBI", account_type="bank", account_ref="9132")
        db.add_all([hdfc, sbi]); db.flush()
        desc = "UPI/DR/123456789012/RAM STORE/HDFC/ramstore@/Payment"
        old = Transaction(account_id=sbi.id, date=date(2026, 10, 1), amount=D(100), transaction_type="debit", description=desc, raw_description=desc, needs_review=True, fingerprint="a")
        new = Transaction(account_id=hdfc.id, date=date(2026, 10, 17), amount=D(5830), transaction_type="debit", description=desc, raw_description=desc, needs_review=True, fingerprint="b")
        db.add_all([old, new]); db.flush()
        db.add(BankAlertRow(owner_id="user", bank="HDFC", account_last4="1632", direction="debit", amount=D(5830), txn_date=date(2026, 10, 17), kind="upi",
                            sent_at=datetime(2026, 10, 17, 9, 42, tzinfo=timezone.utc), status="matched", matched_txn_id=new.id, natural_key="k1"))
        db.commit()
        row = review_list(db)[0]
        self.assertEqual((row["last"]["date"], row["last"]["amount"], row["last"]["direction"], row["last"]["account"]), ("2026-10-17", 5830.0, "debit", "HDFC …1632 (user)"))
        self.assertIsNotNone(row["last"]["time"])                                              # the alert's send time
        self.assertEqual(set(row["accounts"]), {"HDFC …1632 (user)", "SBI …9132 (wife)"})


if __name__ == "__main__":
    unittest.main()
