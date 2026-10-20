"""
A contract note and the Console tradebook report the same trade with the same Trade ID, so (once both carry the exchange trade date)
they share one cashflow row (a Zerodha trade's fingerprint is owner, ISIN, date, amount and direction). Removing the lower-ranked duplicate trade must not remove that shared row: it would leave the kept
trade without a cashflow and XIRR would miss it (37 trades in the first import of the wife's 2020 tradebook).
Needs a scratch Postgres (TEST_DATABASE_URL).
"""
import os
import unittest
from datetime import date
from decimal import Decimal as D

from app.ingestion.parsers.base import ParsedTransaction, ParseResult

URL = os.getenv("TEST_DATABASE_URL")


def trade(kind, qty="19", day=date(2020, 7, 29), ref="77797718", amount="40487.10"):
    return ParsedTransaction(date=day, amount=D(amount), transaction_type="credit", description=f"SELL {qty} × RELIANCE @ 2130.90",
                             raw_description="x", reference_number=ref, counterparty="RELIANCE", isin="INE002A01018",
                             quantity=D(qty), price=D("2130.90"), kind=kind)


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class SharedCashflow(unittest.TestCase):
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
            c.execute(text("TRUNCATE instrument_trades, transactions, demat_movements, isin_aliases, accounts, owners CASCADE"))
        self.db = self.S()
        self.addCleanup(self.db.close)

    def store(self, kind, name):
        from app.etl.normalizer import persist_parse_result
        persist_parse_result(ParseResult(institution="Zerodha", account_type="stocks", source_file=name, transactions=[trade(kind)]), "wife", self.db)
        self.db.commit()

    def test_removing_the_duplicate_trade_keeps_the_cashflow_the_kept_trade_points_at(self):
        from app.ledger.engine import dedupe_cashflows
        self.store("contract_note", "cn.pdf")
        self.store("tradebook", "tb.xlsx")
        trades = self.db.query(self.m.InstrumentTrade).all()
        self.assertEqual(sorted(t.source for t in trades), ["contract_note", "tradebook"])
        self.assertEqual(len({t.txn_fingerprint for t in trades}), 1)                     # the shared cashflow row
        self.assertEqual(self.db.query(self.m.Transaction).count(), 1)
        out = dedupe_cashflows(self.db, apply=True)
        self.assertEqual((out["trades_removed"], out["cashflows_removed"]), (1, 0))
        (kept,) = self.db.query(self.m.InstrumentTrade).all()
        self.assertEqual(kept.source, "contract_note")
        self.assertEqual(self.db.query(self.m.Transaction).filter_by(fingerprint=kept.txn_fingerprint).count(), 1)

    def test_a_duplicate_with_its_own_cashflow_still_loses_it(self):
        from app.ledger.engine import dedupe_cashflows
        self.store("contract_note", "cn.pdf")
        from app.etl.normalizer import persist_parse_result
        tb = trade("tradebook", amount="40487.11")                                       # same trade, a paisa apart: its own cashflow row
        persist_parse_result(ParseResult(institution="Zerodha", account_type="stocks", source_file="tb.xlsx", transactions=[tb]), "wife", self.db)
        self.db.commit()
        self.assertEqual(self.db.query(self.m.Transaction).count(), 2)
        out = dedupe_cashflows(self.db, apply=True)
        self.assertEqual((out["trades_removed"], out["cashflows_removed"]), (1, 1))


    def test_a_re_read_points_an_existing_trade_at_the_current_cashflow_fingerprint(self):
        # The wife's 2018 BEL trades were stored when the cashflow fingerprint was computed differently: they pointed at a row
        # that no longer existed ("trade_without_cashflow") and re-reading the note never repaired it.
        from app.etl.normalizer import persist_parse_result
        self.store("contract_note", "cn.pdf")
        t = self.db.query(self.m.InstrumentTrade).one()
        current = t.txn_fingerprint
        t.txn_fingerprint = "stale-fingerprint"
        self.db.commit()
        persist_parse_result(ParseResult(institution="Zerodha", account_type="stocks", source_file="cn.pdf", transactions=[trade("contract_note")]), "wife", self.db)
        self.db.commit()
        self.assertEqual(self.db.query(self.m.InstrumentTrade).one().txn_fingerprint, current)
        self.assertEqual(self.db.query(self.m.InstrumentTrade).count(), 1)                # still one trade


    def test_maintenance_relinks_a_trade_whose_cashflow_fingerprint_changed(self):
        # a cashflow row's fingerprint changes when its ISIN is filled in later; the trade's link must not be left pointing at nothing
        from app.etl.maintenance import _dedup_transactions
        self.store("contract_note", "cn.pdf")
        x = self.db.query(self.m.Transaction).one()
        t = self.db.query(self.m.InstrumentTrade).one()
        self.assertEqual(t.txn_fingerprint, x.fingerprint)
        x.isin = None; x.fingerprint = "old-style-fingerprint"                       # as if stored under an older identity rule
        self.db.commit()
        _dedup_transactions(self.db)
        t = self.db.query(self.m.InstrumentTrade).one(); x = self.db.query(self.m.Transaction).one()
        self.assertNotEqual(x.fingerprint, "old-style-fingerprint")                  # maintenance re-keyed the row…
        self.assertEqual(t.txn_fingerprint, x.fingerprint)                           # …and the trade followed it

    def test_a_dead_link_is_healed_only_when_exactly_one_cashflow_row_matches(self):
        from app.etl.maintenance import _relink_trades
        self.store("contract_note", "cn.pdf")
        t = self.db.query(self.m.InstrumentTrade).one()
        good = self.db.query(self.m.Transaction).one().fingerprint
        t.txn_fingerprint = "dead"; self.db.commit()
        self.assertEqual(_relink_trades(self.db), 1)
        self.assertEqual(self.db.query(self.m.InstrumentTrade).one().txn_fingerprint, good)
        # two rows with the same date, amount and reference: ambiguous, so nothing is guessed
        x = self.db.query(self.m.Transaction).one()
        self.db.add(self.m.Transaction(account_id=x.account_id, date=x.date, amount=x.amount, transaction_type=x.transaction_type,
                                       reference_number=x.reference_number, description="copy", fingerprint="copy-fp"))
        t = self.db.query(self.m.InstrumentTrade).one(); t.txn_fingerprint = "dead2"; self.db.commit()
        self.assertEqual(_relink_trades(self.db), 0)
        self.assertEqual(self.db.query(self.m.InstrumentTrade).one().txn_fingerprint, "dead2")


if __name__ == "__main__":
    unittest.main()
