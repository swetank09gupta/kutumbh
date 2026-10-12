"""
Spending seen in alert e-mails before the statement arrives is counted, as a floor and marked provisional (ADR-0037).
Real pending alerts: a ₹200,000 transfer to the owner's own HDFC account, SBI sweep debits with no payee and ₹11,500 SIP debits
must NOT become spending; a ₹212.57 UPI payment to Uber must. When the statement lands the alert leaves the total.
Needs a scratch Postgres (TEST_DATABASE_URL).
"""
import os
import unittest
import uuid
from datetime import date
from decimal import Decimal as D
from unittest import mock

URL = os.getenv("TEST_DATABASE_URL")


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class Provisional(unittest.TestCase):
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
            c.execute(text("TRUNCATE bank_alerts, transactions, accounts, owners CASCADE"))
        self.db, m = self.S(), self.m
        self.db.add(m.Owner(id="user", name="Swetank Gupta")); self.db.flush()
        self.bank = m.Account(owner_id="user", institution="HDFC", account_type="bank", account_ref="1632")
        self.db.add(self.bank); self.db.flush()
        self.db.add(m.Transaction(account_id=self.bank.id, date=date(2026, 9, 21), amount=D(100), transaction_type="debit", description="x",
                                  raw_description="x", fingerprint="f0", category="other"))        # the newest statement row: 21 Sep
        self.db.commit()
        self.addCleanup(self.db.close)

    def alert(self, day, amount, payee, kind="upi", direction="debit", vpa=None, ref=None):
        self.db.add(self.m.BankAlertRow(owner_id="user", bank="HDFC", account_last4="1632", direction=direction, amount=D(str(amount)),
                                        txn_date=day, kind=kind, payee=payee, vpa=vpa, ref=ref, status="new",
                                        natural_key=uuid.uuid4().hex))
        self.db.commit()

    def spending(self):
        from sqlalchemy import text
        from app.txn.alert_recon import reconcile_alerts
        reconcile_alerts(self.db)
        return self.db.execute(text("SELECT amount, provisional FROM v_cashflow WHERE flow='expense' AND provisional ORDER BY amount")).all()

    def test_a_pending_upi_payment_to_a_named_payee_is_provisional_spending(self):
        self.alert(date(2026, 9, 24), "212.57", "UBER INDIA SYSTEMS PRIVATE L", vpa="amzn-x@rapl")
        self.assertEqual(self.spending(), [(D("212.57"), True)])

    def test_transfers_to_the_household_sweeps_sips_and_credits_are_not(self):
        self.alert(date(2026, 10, 1), 200000, "Swetan", kind="upi")                         # truncated own name
        self.alert(date(2026, 10, 1), 13225, None, kind="transfer")                         # a sweep: no payee
        self.alert(date(2026, 10, 1), 11500, "", kind="ach")                                # a SIP debit
        self.alert(date(2026, 10, 2), 600, "Mahfuj Ali", direction="credit", vpa="x@nyes")  # money in
        self.alert(date(2026, 10, 2), 1212, "Mr. SWETANKGUPTA", direction="debit")          # the owner's own name
        self.assertEqual(self.spending(), [])

    def test_an_alert_older_than_the_newest_statement_row_is_the_statements_business(self):
        self.alert(date(2026, 9, 10), 99, "Some Shop", vpa="s@ybl")                         # inside the statement's range, no row: 'unmatched'
        self.assertEqual(self.spending(), [])

    def test_when_the_statement_carries_it_the_alert_leaves_the_total(self):
        self.alert(date(2026, 9, 24), "212.57", "UBER INDIA", vpa="u@rapl", ref="UPI-RRN-436501234567")
        self.assertEqual(len(self.spending()), 1)
        self.db.add(self.m.Transaction(account_id=self.bank.id, date=date(2026, 9, 24), amount=D("212.57"), transaction_type="debit",
                                       description="UPI-UBER INDIA-u@rapl-436501234567", raw_description="UPI-UBER INDIA-u@rapl-436501234567 436501234567",
                                       fingerprint="f1", category="transport"))
        self.db.commit()
        self.assertEqual(self.spending(), [])
        from sqlalchemy import text
        n = self.db.execute(text("SELECT COUNT(*) FROM v_cashflow WHERE amount = 212.57")).scalar()
        self.assertEqual(n, 1)                                                              # once, from the statement


if __name__ == "__main__":
    unittest.main()
