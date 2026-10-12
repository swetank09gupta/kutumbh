"""prices_are_live: a fund not valued at the AMFI NAV (or a share not at the market price) is reported, not silently accepted.
Real case: AMFI changed its file layout and every fund fell back to its last trade or a statement rate for days.
Needs a scratch Postgres (TEST_DATABASE_URL)."""
import os
import unittest
from datetime import date
from decimal import Decimal as D

URL = os.getenv("TEST_DATABASE_URL")


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class PricesAreLive(unittest.TestCase):
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
            c.execute(text("TRUNCATE holdings, accounts, owners CASCADE"))
        self.db, m = self.S(), self.m
        self.db.add(m.Owner(id="user", name="u")); self.db.flush()
        self.acct = m.Account(owner_id="user", institution="Zerodha", account_type="stocks", account_ref="z")
        self.db.add(self.acct); self.db.commit()
        self.addCleanup(self.db.close)

    def hold(self, isin, source, name="X"):
        self.db.add(self.m.Holding(account_id=self.acct.id, snapshot_date=date(2026, 10, 12), symbol=name, isin=isin, instrument_name=name,
                                   units=D(10), current_value=D(1000), holding_type="mutual_fund", price_source=source))
        self.db.commit()

    def check(self):
        from app.consistency import prices_are_live
        return prices_are_live(self.db)

    def test_funds_at_amfi_and_shares_at_yahoo_pass(self):
        self.hold("INF000000001", "amfi", "F"); self.hold("INE000000001", "yahoo", "S")
        self.assertTrue(self.check()["ok"])

    def test_a_fund_valued_from_its_last_trade_is_reported(self):
        self.hold("INF000000001", "last_trade", "QUANT TAX")
        c = self.check()
        self.assertEqual((c["ok"], c["severity"], c["values"]["holdings"][0]["valued_from"]), (False, "warn", "last_trade"))

    def test_a_share_with_no_market_price_is_reported_and_a_bond_is_not_judged(self):
        self.hold("INE000000001", "statement", "S"); self.hold("IN0020210178", "statement", "SGB")
        self.assertEqual([x["isin"] for x in self.check()["values"]["holdings"]], ["INE000000001"])


if __name__ == "__main__":
    unittest.main()
