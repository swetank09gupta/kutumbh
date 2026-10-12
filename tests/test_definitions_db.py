"""
Integration test for the shared income/expense definitions and the tagging order of authority.
Needs a scratch Postgres: set TEST_DATABASE_URL (skipped otherwise). Never run it against real data.
"""
import os
import unittest
import uuid
from datetime import date
from decimal import Decimal as D

URL = os.getenv("TEST_DATABASE_URL")


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class Definitions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker
        from app.models import schemas
        from app.models.database import Base
        from app.semantic.definitions import VIEW_SQL
        cls.engine = create_engine(URL)
        from tests.dbhelp import reset_schema
        reset_schema(cls.engine)
        Base.metadata.create_all(cls.engine)
        with cls.engine.begin() as c:
            for stmt in VIEW_SQL:
                c.execute(text(stmt))
        cls.S = sessionmaker(bind=cls.engine)
        cls.schemas = schemas

    def setUp(self):
        from sqlalchemy import text
        with self.engine.begin() as c:
            c.execute(text("TRUNCATE transactions, accounts, owners, payee_memory CASCADE"))
        self.db = self.S()
        m = self.schemas
        self.db.add_all([m.Owner(id="user", name="Swetank Gupta"), m.Owner(id=".archived", name=".archived")])
        self.db.flush()
        self.bank = m.Account(owner_id="user", institution="HDFC", account_type="bank", account_ref="1")
        self.bank2 = m.Account(owner_id="user", institution="SBI", account_type="bank", account_ref="2")
        self.broker = m.Account(owner_id="user", institution="Zerodha", account_type="stocks", account_ref="z")
        self.ghost = m.Account(owner_id=".archived", institution="HDFC", account_type="bank", account_ref="9")
        self.db.add_all([self.bank, self.bank2, self.broker, self.ghost])
        self.db.flush()

    def tearDown(self):
        self.db.close()

    def add(self, acct, typ, amount, cat, sub=None, d=date(2026, 9, 5), desc="x", src=None):
        m = self.schemas
        t = m.Transaction(account_id=acct.id, date=d, amount=D(amount), transaction_type=typ, category=cat,
                          subcategory=sub, description=desc, raw_description=desc, category_source=src,
                          fingerprint=uuid.uuid4().hex)
        self.db.add(t)
        self.db.flush()
        return t

    def flows(self):
        from sqlalchemy import text
        self.db.commit()
        return {r.flow: float(r.total) for r in self.db.execute(
            text("SELECT flow, SUM(amount) total FROM v_cashflow GROUP BY flow"))}

    def test_every_flow_lands_in_exactly_one_bucket(self):
        self.add(self.bank, "credit", 100000, "income", "salary")
        self.add(self.bank, "credit", 500, "shopping")                      # merchant refund → income
        self.add(self.bank, "credit", 7000, "other")                        # unexplained deposit
        self.add(self.bank, "debit", 40000, "transfer", "own_account")
        self.add(self.bank, "debit", 25000, "investment", "mutual_fund")
        self.add(self.bank, "debit", 1060000, "emi", "loan_prepayment")
        self.add(self.bank, "debit", 160000, "emi", "loan_prepayment_other")  # regular EMI
        self.add(self.bank, "debit", 1200, "shopping", "online")
        f = self.flows()
        self.assertEqual(f["income"], 100500)
        self.assertEqual(f["other_credit"], 7000)
        self.assertEqual(f["transfer"], 40000)
        self.assertEqual(f["investment"], 25000)
        self.assertEqual(f["prepayment"], 1060000)
        self.assertEqual(f["expense"], 161200)

    def test_broker_rows_and_archived_owners_never_reach_cash_flow(self):
        self.add(self.broker, "debit", 99999, "other")
        self.add(self.ghost, "credit", 88888, "income")
        self.assertEqual(self.flows(), {})

    def test_monthly_view_matches_the_row_view(self):
        from sqlalchemy import text
        self.add(self.bank, "credit", 100, "income")
        self.add(self.bank, "debit", 30, "food")
        self.add(self.bank, "debit", 20, "food", d=date(2026, 8, 5))
        self.db.commit()
        rows = {str(r.month): (float(r.income), float(r.expense)) for r in
                self.db.execute(text("SELECT month, income, expense FROM v_cashflow_monthly"))}
        self.assertEqual(rows, {"2026-09-01": (100.0, 30.0), "2026-08-01": (0.0, 20.0)})

    def test_user_label_survives_retagging_and_applies_to_every_matching_payment(self):
        from app.txn.pipeline import label_payee, retag
        a = self.add(self.bank, "debit", 4000, "other", desc="UPI-DAMAN MAKHIJA-damanmakhi@ybl-HDFC0001-123-UPI")
        b = self.add(self.bank2, "debit", 1500, "other", d=date(2026, 7, 1),
                     desc="UPI-DAMAN MAKHIJA-damanmakhi@ybl-HDFC0001-999-UPI")
        self.db.commit()
        out = label_payee(self.db, "DAMANMAKHIJA", "medical", None)
        self.assertEqual(out["transactions_updated"], 2)
        retag(self.db, use_llm=False)                                       # must not undo the label
        self.db.expire_all()
        for t in (self.db.get(self.schemas.Transaction, a.id), self.db.get(self.schemas.Transaction, b.id)):
            self.assertEqual((t.category, t.category_source, t.needs_review), ("medical", "user", False))

    def test_unknown_payee_is_queued_for_review_not_guessed(self):
        from app.txn.pipeline import retag, review_list
        self.add(self.bank, "debit", 2500, "other", desc="UPI-RAJESH KUMAR-rajesh@ybl-HDFC0001-555-UPI")
        self.db.commit()
        retag(self.db, use_llm=False)
        rows = review_list(self.db)
        self.assertEqual([r["payee_key"] for r in rows], ["RAJESHKUMAR"])
        self.assertIsNone(rows[0]["suggestion"])


if __name__ == "__main__":
    unittest.main()
