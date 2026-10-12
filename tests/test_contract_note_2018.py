"""The 2018 Zerodha contract-note layout (no exchange token, no ISIN) and resolving a missing ISIN from the same ticker."""
import os
import unittest
import uuid
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace as NS

from app.ingestion.parsers.zerodha import ZerodhaParser

URL = os.getenv("TEST_DATABASE_URL")

NOTE_2018 = """NSE-EQ
1000000005532698 14:31:55 2196789 14:31:55 BEL B 5 133.15 133.1500 (665.75)
Sub Total: 5 (665.75)
Net Total: (665.75)"""
NOTE_2022 = """1000000010564868 14:22:19 53832611 15:16:51 LICI - EQ / B NSE 2 801.00 801.00 (1602.00)
INE0J1Y01017"""
NOTE_2024 = "1200000010564868 14:22:19 53832611 15:16:51 EMUDHRA-EQ/INE01QM01018 B NSE 3 875.00 875 (2625.00)"


class Layouts(unittest.TestCase):
    def parse(self, text):
        return ZerodhaParser()._parse_contract_note_text(text, date(2018, 4, 27))

    def test_the_2018_row_without_an_exchange_token_is_read(self):
        (t,) = self.parse(NOTE_2018)
        self.assertEqual((t.counterparty, t.transaction_type, t.quantity, t.price, t.amount, t.isin),
                         ("BEL", "debit", D(5), D("133.15"), D("665.75"), None))

    def test_the_2022_and_2024_layouts_still_read(self):
        (a,) = self.parse(NOTE_2022)
        (b,) = self.parse(NOTE_2024)
        self.assertEqual((a.counterparty, a.isin, a.quantity), ("LICI", "INE0J1Y01017", D(2)))
        self.assertEqual((b.counterparty, b.isin, b.amount), ("EMUDHRA", "INE01QM01018", D("2625.00")))


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class IsinFromTicker(unittest.TestCase):
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
            c.execute(text("TRUNCATE instrument_trades, isin_aliases, accounts, owners CASCADE"))
        self.db, m = self.S(), self.m
        self.db.add(m.Owner(id="wife", name="w")); self.db.flush()
        self.acct = m.Account(owner_id="wife", institution="Zerodha", account_type="stocks", account_ref="z")
        self.db.add(self.acct); self.db.commit()
        self.addCleanup(self.db.close)

    def known(self, symbol, isin):
        self.db.add(self.m.InstrumentTrade(owner_id="wife", account_id=self.acct.id, isin=isin, symbol=symbol, instrument_name=symbol,
                                           holding_type="equity", trade_date=date(2020, 6, 1), side="B", quantity=D(1), price=D(100),
                                           source="contract_note", natural_key=uuid.uuid4().hex))
        self.db.commit()

    def record(self, symbol):
        from app.ledger.store import record_parse_result
        pt = NS(kind="contract_note", transaction_type="debit", isin=None, counterparty=symbol, raw_description=symbol,
                date=date(2018, 4, 27), quantity=D(5), price=D("133.15"), amount=D("665.75"), reference_number="2196789",
                settle_date=None, seq=None, running_qty=None)
        record_parse_result(self.db, NS(transactions=[pt], source_file="old", statement_date=None), "wife", self.acct, ["fp"])
        self.db.commit()
        return self.db.query(self.m.InstrumentTrade).filter_by(trade_date=date(2018, 4, 27)).one()

    def test_a_note_with_no_isin_takes_the_one_the_same_ticker_has_elsewhere(self):
        self.known("BEL", "INE263A01024"); self.known("BEL-A", "INE263A01024")             # series suffixes are the same ticker
        self.assertEqual(self.record("BEL").isin, "INE263A01024")

    def test_an_ambiguous_ticker_stays_unresolved(self):
        self.known("XYZ", "INE000X01010"); self.known("XYZ", "INE000Y01010")
        self.assertIsNone(self.record("XYZ").isin)

    def test_an_unknown_ticker_stays_unresolved(self):
        self.assertIsNone(self.record("NEVERTRADED").isin)


if __name__ == "__main__":
    unittest.main()
