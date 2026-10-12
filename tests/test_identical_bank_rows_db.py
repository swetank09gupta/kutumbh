"""
Two identical reference-less bank rows are two transactions when the statement's running balance says so.
Real case: the wife's SBI statement of Nov 2024 lists "SWEEP TRF CREDT 1016.00" twice on 24 Nov (balances 1744.77 and
1531.77); the narration-hash identity stored one and the account's balance chain was ₹1,016 off for years.
A row listed twice with the SAME balance is still one row. Needs a scratch Postgres (TEST_DATABASE_URL).
"""
import os
import unittest
from datetime import date
from decimal import Decimal as D

from app.ingestion.parsers.base import ParsedTransaction, ParseResult

URL = os.getenv("TEST_DATABASE_URL")


def row(bal, amount="1016.00", desc="SWEEP TRF CREDT", ref=None, kind="credit"):
    return ParsedTransaction(date=date(2024, 11, 24), amount=D(amount), transaction_type=kind, description=desc,
                             raw_description=desc, reference_number=ref, balance_after=D(bal) if bal is not None else None)


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class IdenticalBankRows(unittest.TestCase):
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
            c.execute(text("TRUNCATE transactions, balances, accounts, owners CASCADE"))
        self.db = self.S()
        self.addCleanup(self.db.close)

    def store(self, *rows, name="s.pdf"):
        from app.etl.normalizer import persist_parse_result
        res = ParseResult(institution="SBI", account_type="bank", account_ref="9132", transactions=list(rows), source_file=name)
        persist_parse_result(res, "wife", self.db)
        self.db.commit()

    def count(self):
        return self.db.query(self.m.Transaction).count()

    def test_two_identical_rows_with_different_balances_are_two_transactions(self):
        self.store(row("1744.77"), row("1531.77"))
        self.assertEqual(self.count(), 2)

    def test_reading_the_statement_again_adds_nothing(self):
        self.store(row("1744.77"), row("1531.77"))
        self.store(row("1744.77"), row("1531.77"), name="again.pdf")
        self.assertEqual(self.count(), 2)

    def test_a_row_listed_twice_with_the_same_balance_is_one_row(self):
        self.store(row("1744.77"), row("1744.77"))
        self.assertEqual(self.count(), 1)

    def test_without_a_balance_nothing_is_assumed(self):
        self.store(row(None), row(None))
        self.assertEqual(self.count(), 1)

    def test_rows_with_a_reference_keep_their_old_identity(self):
        self.store(row("1744.77", ref="UTR1234567890"), row("1531.77", ref="UTR1234567890"))
        self.assertEqual(self.count(), 1)

    def test_maintenance_does_not_merge_the_proven_second_row_away(self):
        from app.etl.maintenance import _dedup_transactions
        self.store(row("1744.77"), row("1531.77"))
        _dedup_transactions(self.db)
        self.assertEqual(self.count(), 2)


if __name__ == "__main__":
    unittest.main()
