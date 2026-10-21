"""SBI's alert 'available balance' includes the owner's linked deposits (Manual Assets of their own): the bank balance takes them out so they are not counted twice. Scratch database."""
import os
import unittest
from datetime import date, timedelta
from decimal import Decimal as D
from unittest import mock

URL = os.getenv("TEST_DATABASE_URL")


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class SbiAlertBalance(unittest.TestCase):
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

    def test_the_print_less_live_deposits_is_the_savings_balance(self):
        from app.models.schemas import Account, BankAlertRow, ManualAsset
        from app.txn.alert_recon import alert_balance
        db = self.db
        sbi = Account(owner_id="user", institution="SBI", account_type="bank", account_ref="3316")
        hdfc = Account(owner_id="user", institution="HDFC", account_type="bank", account_ref="1632")
        db.add_all([sbi, hdfc])
        far, past = date.today() + timedelta(days=200), date.today() - timedelta(days=5)
        for name, owner, val, mat in (("FD a", "user", 500000, far), ("FD b", "user", 400000, far), ("FD matured", "user", 100000, past), ("FD hers", "wife", 300000, far)):
            db.add(ManualAsset(owner_id=owner, asset_type="fd", institution="SBI", name=name, current_value=D(val), maturity_date=mat))
        today = date.today()
        for bank, last4, bal in (("SBI", "3316", 1161000), ("HDFC", "1632", 37000)):
            db.add(BankAlertRow(owner_id="user", bank=bank, account_last4=last4, direction="debit", amount=D(100), txn_date=today, kind="transfer",
                                balance_after=D(bal), status="pending", natural_key=f"k-{bank}"))
        db.commit()
        self.assertEqual(alert_balance(db, sbi)[0], D(261000))      # 1,161,000 less the two live deposits (the matured one is in savings; hers is hers)
        self.assertEqual(alert_balance(db, hdfc)[0], D(37000))      # HDFC prints the account's own balance


if __name__ == "__main__":
    unittest.main()
