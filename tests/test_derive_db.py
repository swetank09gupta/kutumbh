"""
Derivation end to end on a scratch database, built from a real case: a fund whose first statement already holds
22.752 units no trade explains (bought by a 26-Jun ₹10,000 UPI debit), while a demat credit of 22.566 units on
3 Jul belongs to the 1-Jul ₹10,000 mandate debit. Both debits have the same amount; the match must not swap them.
Needs a scratch Postgres (TEST_DATABASE_URL).
"""
import os
import unittest
import uuid
from datetime import date, timedelta
from decimal import Decimal as D
from unittest import mock

URL = os.getenv("TEST_DATABASE_URL")
ISIN = "INF966L01986"


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class DeriveWorld(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.models import schemas
        from app.models.database import Base
        from tests.dbhelp import reset_schema
        cls.engine = create_engine(URL)
        reset_schema(cls.engine)
        Base.metadata.create_all(cls.engine)
        cls.S, cls.m = sessionmaker(bind=cls.engine), schemas

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        from sqlalchemy import text
        with self.engine.begin() as c:
            c.execute(text("TRUNCATE instrument_trades, demat_movements, statement_balances, transactions, accounts, owners CASCADE"))
        self.db, m = self.S(), self.m
        self.db.add(m.Owner(id="wife", name="Akanksha Arya"))
        self.db.flush()
        bank = m.Account(owner_id="wife", institution="SBI", account_type="bank", account_ref="9132")
        coin = m.Account(owner_id="wife", institution="Zerodha Coin", account_type="mutual_fund", account_ref="c")
        zer = m.Account(owner_id="wife", institution="Zerodha", account_type="stocks", account_ref="z")
        self.db.add_all([bank, coin, zer]); self.db.flush()
        self.coin = coin

        def debit(d, desc):
            self.db.add(m.Transaction(account_id=bank.id, date=d, amount=D(10000), transaction_type="debit",
                                      description=desc, raw_description=desc, fingerprint=uuid.uuid4().hex))
        debit(date(2024, 6, 26), "UPI/DR/417809484180/ICCL ZER/HDFC/zerodhamf@/Bzu8H")
        debit(date(2024, 7, 1), "ACHDr YESB00709000028661 INDIAN CLEARIN")
        # the first monthly statement (Dec 2024) already shows 22.752 + 22.566 units; only the Jul credit has a line
        self.db.add(m.StatementBalance(owner_id="wife", family="zerodha", as_on=date(2024, 12, 31), isin=ISIN,
                                       qty=D("45.318"), name="QUANT TAX"))
        self.db.add(m.DematMovement(owner_id="wife", account_id=zer.id, isin=ISIN, symbol="QUANT", move_date=date(2024, 7, 3),
                                    qty_in=D("22.566"), qty_out=D(0), description="CDSL alert *08489463 19:11:27 QUANT MF",
                                    source_file="alert", natural_key=uuid.uuid4().hex))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    NAV = {date(2024, 6, 26): D("439.5002"), date(2024, 7, 1): D("443.6000"), date(2024, 7, 2): D("443.1100")}

    @staticmethod
    def _nav(table):
        def f(isin, d):
            for k in range(8):
                x = d - timedelta(days=k)
                if x in table:
                    return x, table[x]
            return None
        return f

    def test_each_purchase_takes_its_own_debit(self):
        from app.ledger.derive import derive_for_owner
        with mock.patch("app.ledger.derive.nav_on_or_before", self._nav(self.NAV)):
            made = derive_for_owner(self.db, "wife")
        trades = {str(t.settle_date): t for t in self.db.query(self.m.InstrumentTrade).filter_by(source="derived")}
        self.assertEqual(made, 2)
        credit = trades["2024-07-03"]
        self.assertEqual(credit.quantity, D("22.566"))
        self.assertEqual(credit.trade_date, date(2024, 7, 2))
        opening = next(t for k, t in trades.items() if k != "2024-07-03")
        self.assertEqual(opening.quantity, D("22.752"))
        self.assertEqual(opening.trade_date, date(2024, 6, 26))               # the 26-Jun UPI debit, not the 1-Jul one
        refs = {t.source_ref for t in trades.values()}
        self.assertEqual(len(refs), 2)                                          # two different bank debits confirm them
        self.assertTrue(all(r.startswith("bank:") for r in refs))

    def test_derivation_is_repeatable_and_leaves_no_orphans(self):
        from app.ledger.derive import derive_all
        with mock.patch("app.ledger.derive.nav_on_or_before", self._nav(self.NAV)):
            derive_all(self.db)
            derive_all(self.db)
        self.assertEqual(self.db.query(self.m.InstrumentTrade).filter_by(source="derived").count(), 2)
        self.assertEqual(self.db.query(self.m.Transaction).filter_by(source_file="derived:demat+nav").count(), 2)


if __name__ == "__main__":
    unittest.main()
