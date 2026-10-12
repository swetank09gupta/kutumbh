"""
A Coin order must be stored once, however often its mail is re-read and whatever ISIN it was filed under.
Real case: an ICICI Large Cap SIP stored under a wrong ISIN, corrected in place by a learned alias (the natural key kept the
old ISIN); the next 7-day rescan parsed the same mail under the right ISIN and added a second copy (+89 units, +₹10k).
Needs a scratch Postgres (TEST_DATABASE_URL).
"""
import os
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace as NS

URL = os.getenv("TEST_DATABASE_URL")
WRONG, RIGHT = "INF109K01BN0", "INF109K016L0"


def parsed(isin, day=date(2026, 10, 5), qty="89.051", amount="9999.50", ref="39235364/13", kind="coin"):
    return NS(kind=kind, transaction_type="debit", isin=isin, counterparty="ICICI PRUDENTIAL LARGE CAP FUND", raw_description="x",
              date=day, quantity=D(qty), price=None, amount=D(amount), reference_number=ref, settle_date=None, seq=None,
              running_qty=None)


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class CoinOrderIsStoredOnce(unittest.TestCase):
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
        self.db.add(m.Owner(id="user", name="u")); self.db.flush()
        self.acct = m.Account(owner_id="user", institution="Coin", account_type="mf", account_ref="c")
        self.db.add(self.acct); self.db.commit()
        self.addCleanup(self.db.close)

    def record(self, *pts):
        from app.ledger.store import record_parse_result
        res = NS(transactions=list(pts), source_file="data/raw/x", statement_date=None)
        out = record_parse_result(self.db, res, "user", self.acct, ["fp"] * len(pts))
        self.db.commit()
        return out["trades"]

    def count(self):
        return self.db.query(self.m.InstrumentTrade).count()

    def test_reading_the_same_mail_again_adds_nothing(self):
        self.assertEqual(self.record(parsed(RIGHT)), 1)
        self.assertEqual(self.record(parsed(RIGHT)), 0)
        self.assertEqual(self.count(), 1)

    def test_an_isin_corrected_in_place_does_not_make_the_next_rescan_insert_a_copy(self):     # the shipped bug
        self.record(parsed(WRONG))
        self.db.query(self.m.InstrumentTrade).update({"isin": RIGHT})         # what a learned alias does: key keeps the old ISIN
        self.db.commit()
        self.assertEqual(self.record(parsed(RIGHT)), 0)
        self.assertEqual(self.count(), 1)

    def test_a_genuinely_different_order_is_still_stored(self):
        self.record(parsed(RIGHT))
        self.assertEqual(self.record(parsed(RIGHT, day=date(2026, 11, 5)), parsed(RIGHT, amount="5000.00", qty="44.0")), 2)
        self.assertEqual(self.count(), 3)

    def test_other_sources_are_not_affected(self):
        self.record(parsed(RIGHT, kind="contract_note", ref="53252601"))
        self.assertEqual(self.record(parsed(WRONG, kind="contract_note", ref="53252601")), 1)   # not a Coin order: old rule applies

    def _add(self, isin, key, created, ref="39235364/13", source="coin"):
        self.db.add(self.m.InstrumentTrade(
            owner_id="user", account_id=self.acct.id, isin=isin, instrument_name="X", holding_type="mutual_fund",
            trade_date=date(2026, 10, 5), side="B", quantity=D("89.051"), amount=D("9999.50"), source=source, source_ref=ref,
            natural_key=key, created_at=created))

    def test_an_existing_copy_is_cleaned_up_keeping_the_first(self):
        from app.etl.maintenance import _dedupe_coin_trades
        t0 = datetime(2026, 10, 11, tzinfo=timezone.utc)
        self._add(RIGHT, "k1", t0)
        self._add(RIGHT, "k2", t0 + timedelta(days=1))
        self._add(RIGHT, "k3", t0, ref=None)                                   # no folio: cannot be proven the same order
        self._add(RIGHT, "k4", t0 + timedelta(days=1), ref=None)
        self._add(RIGHT, "k5", t0, ref="53252601", source="contract_note")     # other source: untouched
        self._add(RIGHT, "k6", t0 + timedelta(days=1), ref="53252601", source="contract_note")
        self.db.commit()
        self.assertEqual(_dedupe_coin_trades(self.db), {"removed": 1})
        self.assertEqual(sorted(t.natural_key for t in self.db.query(self.m.InstrumentTrade)), ["k1", "k3", "k4", "k5", "k6"])

    def test_the_cleanup_runs_with_every_update(self):
        from app.etl import maintenance
        import inspect
        self.assertIn('"dedupe_coin_trades"', inspect.getsource(maintenance.run_all))


if __name__ == "__main__":
    unittest.main()
