"""
Integration test for bank-alert reconciliation. Needs a scratch Postgres (TEST_DATABASE_URL); never real data.
"""
import os
import unittest
import uuid
from datetime import date
from decimal import Decimal as D

URL = os.getenv("TEST_DATABASE_URL")


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class AlertRecon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker
        from app.models import schemas
        from app.models.database import Base
        cls.engine = create_engine(URL)
        from tests.dbhelp import reset_schema
        reset_schema(cls.engine)
        Base.metadata.create_all(cls.engine)
        cls.S, cls.m = sessionmaker(bind=cls.engine), schemas

    def setUp(self):
        from sqlalchemy import text
        with self.engine.begin() as c:
            c.execute(text("TRUNCATE bank_alerts, transactions, accounts, owners CASCADE"))
        self.db, m = self.S(), self.m
        self.db.add(m.Owner(id="user", name="Swetank Gupta"))
        self.db.flush()
        self.hdfc = m.Account(owner_id="user", institution="HDFC", account_type="bank", account_ref="1632")
        self.sbi = m.Account(owner_id="user", institution="SBI", account_type="bank", account_ref="8660")
        self.db.add_all([self.hdfc, self.sbi])
        self.db.flush()

    def tearDown(self):
        self.db.close()

    def txn(self, acct, d, amt, kind, raw):
        self.db.add(self.m.Transaction(account_id=acct.id, date=d, amount=D(amt), transaction_type=kind,
                                       raw_description=raw, fingerprint=uuid.uuid4().hex))

    def alert(self, bank, last4, d, amt, direction, kind="upi", ref=None):
        self.db.add(self.m.BankAlertRow(owner_id="user", bank=bank, account_last4=last4, direction=direction,
                                        amount=D(amt), txn_date=d, kind=kind, ref=ref,
                                        natural_key=uuid.uuid4().hex))

    def statuses(self):
        from app.txn.alert_recon import reconcile_alerts
        reconcile_alerts(self.db)
        return sorted((a.bank, a.account_last4, str(a.txn_date), a.status) for a in self.db.query(self.m.BankAlertRow))

    def test_statuses(self):
        self.txn(self.hdfc, date(2026, 9, 20), "180", "debit", "UPI-SANTOSH-X@OKSBI-SBIN-129953566390-UPI")
        self.txn(self.hdfc, date(2026, 9, 21), "25", "debit", "UPI-NSP FOOD-Y@PTY-YESB-129993430292-UPI")
        self.alert("HDFC", "1632", date(2026, 9, 20), "180", "debit", ref="129953566390")        # matched by UTR
        self.alert("HDFC", "1632", date(2026, 9, 21), "999", "debit", ref="555555555555")        # inside statement, no row
        self.alert("HDFC", "1632", date(2026, 10, 3), "50", "debit", ref="777777777777")         # after statement
        self.alert("HDFC", "1632", date(2026, 9, 21), "25", "debit")                              # ref-less date+amount match
        self.db.flush()
        got = {(d, s) for _, _, d, s in self.statuses()}
        self.assertEqual(got, {("2026-09-20", "matched"), ("2026-09-21", "matched"),
                               ("2026-09-21", "unmatched"), ("2026-10-03", "pending")})

    def test_an_alert_in_a_month_whose_statement_never_arrived_is_a_gap_not_a_lost_row(self):
        # statements for Sep and Nov exist, October's was never mailed: its alerts have nothing to match against
        self.txn(self.hdfc, date(2026, 9, 20), "10", "debit", "UPI-A")
        self.txn(self.hdfc, date(2026, 11, 5), "10", "debit", "UPI-B")
        self.alert("HDFC", "1632", date(2026, 10, 9), "400", "debit", ref="333333333333")          # October: no statement
        self.alert("HDFC", "1632", date(2026, 11, 5), "999", "debit", ref="444444444444")          # November has rows but not this one
        self.db.flush()
        got = {(d, s) for _, _, d, s in self.statuses()}
        self.assertEqual(got, {("2026-10-09", "gap"), ("2026-11-05", "unmatched")})

    def test_deposit_alerts_older_than_the_statement_or_long_silent_are_not_warnings(self):
        from datetime import timedelta
        from app.txn.alert_recon import alert_summary, reconcile_alerts
        today = date.today()
        def dep(last4, days_ago, bal):                                                           # an untracked deposit account's alert
            self.db.add(self.m.BankAlertRow(owner_id="user", bank="SBI", account_last4=last4, direction="debit", amount=D("10"),
                                            txn_date=today - timedelta(days=days_ago), kind="transfer", balance_after=D(bal),
                                            natural_key=uuid.uuid4().hex))
        dep("4895", 300, "55000"); dep("9764", 400, "18000"); dep("9858", 20, "12000")
        self.db.add(self.m.ManualAsset(owner_id="user", asset_type="fd", name="SBI FD ...4895", current_value=D("40000"),
                                       source_statement_date=today - timedelta(days=30)))
        self.db.flush(); reconcile_alerts(self.db)
        out = alert_summary(self.db)
        self.assertEqual(out["deposit_accounts"], [])                                              # 4895: the newer statement wins
        self.assertEqual([r["account"] for r in out["accounts_missing_from_assets"]], ["9858"])    # 9764 silent 400 days: closed

    def test_one_statement_row_confirms_one_alert(self):
        self.txn(self.hdfc, date(2026, 9, 20), "35", "debit", "UPI-A-a@ybl-1-UPI")
        self.txn(self.hdfc, date(2026, 9, 25), "1", "debit", "UPI-pad")
        self.alert("HDFC", "1632", date(2026, 9, 20), "35", "debit", ref="111111111111")
        self.alert("HDFC", "1632", date(2026, 9, 20), "35", "debit", ref="222222222222")
        self.db.flush()
        res = [s for *_, s in self.statuses()]
        self.assertEqual(sorted(res), ["matched", "unmatched"])

    def test_announced_twice_is_not_a_second_event(self):
        self.txn(self.sbi, date(2026, 8, 1), "200000", "debit", "NEFT")
        self.txn(self.sbi, date(2026, 8, 31), "1", "debit", "pad")
        self.alert("SBI", "3316", date(2026, 8, 1), "200000", "debit", kind="neft", ref="SBIN526152155683")
        self.alert("SBI", "3316", date(2026, 8, 1), "200000", "debit", kind="transfer")
        self.db.flush()
        self.assertEqual(sorted(s for *_, s in self.statuses()), ["dup", "matched"])

    def test_account_without_statements_is_untracked_not_a_parse_gap(self):
        self.txn(self.sbi, date(2026, 8, 31), "1", "debit", "pad")
        self.txn(self.sbi, date(2026, 8, 1), "500", "debit", "x")
        self.alert("SBI", "3316", date(2026, 8, 1), "500", "debit", kind="transfer")      # matched → 3316 is tracked
        self.alert("SBI", "0868", date(2026, 8, 15), "124", "debit", kind="transfer")     # RD account: no statements
        self.db.flush()
        got = {(l4, s) for _, l4, _, s in self.statuses()}
        self.assertEqual(got, {("3316", "matched"), ("0868", "untracked")})


if __name__ == "__main__":
    unittest.main()
