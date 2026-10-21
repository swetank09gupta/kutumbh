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
            c.execute(text("TRUNCATE transactions, balances, bank_alerts, accounts, owners CASCADE"))
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

    def test_a_reversal_credit_with_the_debits_reference_is_stored_too(self):
        # Real case: HDFC "REV-UPI" credit of a failed ₹50,000 payment carries the debit's date and UPI reference.
        self.store(row("143037.99", "50000.00", "UPI-ZERODHA", "102738290", "debit"),
                   row("193037.99", "50000.00", "REV-UPI-ZERODHA", "102738290", "credit"))
        self.assertEqual(self.count(), 2)

    def test_maintenance_never_changes_a_broker_rows_fingerprint(self):
        # trades link to their cashflow by transactions.fingerprint; a re-key of broker rows left 216 trades without a cashflow
        from app.etl.maintenance import _dedup_transactions
        from app.etl.normalizer import persist_parse_result
        persist_parse_result(ParseResult(institution="Zerodha", account_type="stocks", source_file="cn.pdf", transactions=[
            ParsedTransaction(date=date(2020, 7, 31), amount=D("40487.10"), transaction_type="credit", description="SELL 19 × RELIANCE @ 2130.90",
                              raw_description="x", reference_number="CN123", counterparty="RELIANCE", quantity=D("19"), price=D("2130.90"), kind="contract_note")]),
            "wife", self.db)
        self.db.commit()
        before = self.db.query(self.m.Transaction.fingerprint).scalar()
        _dedup_transactions(self.db)
        self.assertEqual(self.db.query(self.m.Transaction.fingerprint).scalar(), before)

    def test_maintenance_rekeys_a_ref_only_row_and_keeps_the_reversal(self):
        from app.etl.maintenance import _dedup_transactions
        import hashlib
        self.store(row("143037.99", "50000.00", "UPI-ZERODHA", "102738290", "debit"))
        t = self.db.query(self.m.Transaction).one()
        t.fingerprint = hashlib.sha256(f"{t.account_id}|{t.date}|102738290".encode()).hexdigest()   # the old identity
        self.db.commit()
        self.store(row("193037.99", "50000.00", "REV-UPI-ZERODHA", "102738290", "credit"), name="again.pdf")
        _dedup_transactions(self.db)
        self.assertEqual(self.count(), 2)
        self.assertEqual(len({r.fingerprint for r in self.db.query(self.m.Transaction)}), 2)

    def test_a_break_after_a_month_without_statement_names_that_month(self):
        # Real case: SBI 8660, no statement for Oct 2024; Nov opens ₹47,160 lower than Sep closed.
        from app.txn.continuity import account_breaks
        def at(day, bal, amount="100.00"):
            return ParsedTransaction(date=day, amount=D(amount), transaction_type="debit", description=f"UPI {day}",
                                     raw_description=f"UPI {day}", reference_number=f"R{day:%m%d}", balance_after=D(bal))
        self.store(at(date(2024, 9, 28), "1000.00"), at(date(2024, 9, 30), "900.00"), name="sep.pdf")
        self.store(at(date(2024, 11, 1), "300.00"), at(date(2024, 11, 5), "200.00"), name="nov.pdf")   # 500 was spent in October
        (acct,) = [a for a in account_breaks(self.db) if a["breaks"]]
        self.assertEqual([(b["date"], b.get("gap")) for b in acct["breaks"]], [(date(2024, 11, 1), "2024-10")])

    def _october_gap(self):
        def at(day, bal):
            return ParsedTransaction(date=day, amount=D("100.00"), transaction_type="debit", description=f"UPI {day}",
                                     raw_description=f"UPI {day}", reference_number=f"R{day:%m%d}", balance_after=D(bal))
        self.store(at(date(2024, 9, 28), "1000.00"), at(date(2024, 9, 30), "900.00"), name="sep.pdf")
        self.store(at(date(2024, 11, 1), "300.00"), at(date(2024, 11, 5), "200.00"), name="nov.pdf")      # ₹500 net spent in October

    def _alert(self, n, direction, amount, status="new", day=date(2024, 10, 9)):
        self.db.add(self.m.BankAlertRow(owner_id="wife", bank="SBI", account_last4="9132", direction=direction, amount=D(amount),
                                        txn_date=day, kind="upi", natural_key=f"k{n}", status=status))
        self.db.commit()

    def test_a_missing_month_is_explained_when_the_banks_alerts_add_up_to_it_exactly(self):
        from app.txn.continuity import account_breaks
        self._october_gap()
        for n, (d, a) in enumerate((("debit", "400.00"), ("debit", "200.00"), ("credit", "100.00"))):
            self._alert(n, d, a)
        self._alert(9, "debit", "400.00", status="dup")                          # the same alert announced twice: not counted again
        (b,) = [x for a in account_breaks(self.db) for x in a["breaks"]]
        self.assertEqual((b["gap"], b["alerts"]["count"], b["alerts"]["net_debit"], b["explained"]), ("2024-10", 3, D("500.00"), True))

    def test_alerts_that_do_not_add_up_leave_the_break_open_and_say_how_far_they_get(self):
        from app import consistency
        from app.txn.continuity import account_breaks
        self._october_gap()
        self._alert(1, "debit", "400.00")
        (b,) = [x for a in account_breaks(self.db) for x in a["breaks"]]
        self.assertFalse(b["explained"])
        c = consistency.bank_balances_chain(self.db)
        self.assertFalse(c["ok"]); self.assertIn("1 alert(s) show a net debit of ₹400.00, the break is a net debit of ₹500.00", c["detail"])

    def test_a_month_the_owner_confirmed_never_existed_is_not_a_warning(self):
        import json, os, tempfile
        from unittest import mock
        from app import consistency
        self._october_gap()
        self.assertFalse(consistency.bank_balances_chain(self.db)["ok"])
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"ACCEPTED_GAPS_FILE": f"{d}/g.json"}):
            open(f"{d}/g.json", "w").write(json.dumps([{"owner": "wife", "account": "SBI 9132", "month": "2024-10", "note": "never issued"}]))
            c = consistency.bank_balances_chain(self.db)
            self.assertTrue(c["ok"]); self.assertEqual(c["values"]["accepted"][0]["month"], "2024-10")
            open(f"{d}/g.json", "w").write(json.dumps([{"owner": "wife", "account": "SBI 9132", "month": "2024-09"}]))     # another month: still a warning
            self.assertFalse(consistency.bank_balances_chain(self.db)["ok"])
            open(f"{d}/g.json", "w").write("not json")                                                                     # unreadable: nothing is accepted
            self.assertFalse(consistency.bank_balances_chain(self.db)["ok"])

    def test_an_explained_month_is_not_a_warning(self):
        from app import consistency
        self._october_gap()
        self._alert(1, "debit", "600.00"); self._alert(2, "credit", "100.00")
        c = consistency.bank_balances_chain(self.db)
        self.assertTrue(c["ok"]); self.assertEqual([e["month"] for e in c["values"]["explained"]], ["2024-10"])

    def test_a_break_inside_a_covered_month_is_not_called_a_missing_statement(self):
        from app.txn.continuity import account_breaks
        def at(day, bal):
            return ParsedTransaction(date=day, amount=D("100.00"), transaction_type="debit", description=f"UPI {day}",
                                     raw_description=f"UPI {day}", reference_number=f"R{day:%m%d}", balance_after=D(bal))
        self.store(at(date(2024, 10, 28), "1000.00"), at(date(2024, 11, 2), "500.00"), name="x.pdf")   # 400 missing, Oct has rows
        (acct,) = [a for a in account_breaks(self.db) if a["breaks"]]
        self.assertIsNone(acct["breaks"][0].get("gap"))

    def test_freshness_follows_each_accounts_own_statement_rhythm_and_ignores_zero_balances(self):
        from datetime import timedelta
        from app import consistency
        from app.etl.normalizer import persist_parse_result
        from app.ingestion.parsers.base import ParsedBalance
        today = date.today()
        def balances(ref, ages, amount):                      # ages: days ago of each monthly statement balance
            for age in ages:
                persist_parse_result(ParseResult(institution="SBI", account_type="bank", account_ref=ref, source_file=f"{ref}{age}.pdf",
                                                 balance=ParsedBalance(balance=D(amount), snapshot_date=today - timedelta(days=age))), "wife", self.db)
        balances("1111", (50, 80, 110, 140), "5000")          # monthly, last one 50 days ago: next is still due
        balances("2222", (95, 125, 155, 185), "5000")         # monthly, last one 95 days ago: missed
        balances("3333", (400, 430, 460), "0")                # zero: cannot change net worth
        self.db.commit()
        out = consistency.data_freshness(self.db)
        self.assertEqual([a["account"] for a in out["values"]["accounts"]], ["SBI 2222"])

    def test_maintenance_merges_two_masks_of_one_account(self):
        from app.etl.maintenance import _merge_masked_accounts
        from app.ingestion.parsers.base import ParsedBalance
        from app.etl.normalizer import persist_parse_result
        for ref, day in (("50410XXXXX", date(2025, 11, 30)), ("XXXXXX3237", date(2024, 9, 30))):
            persist_parse_result(ParseResult(institution="Axis", account_type="bank", account_ref=ref, source_file=f"{ref}.pdf",
                                             balance=ParsedBalance(balance=D("0"), snapshot_date=day)), "user", self.db)
        self.db.commit()
        self.assertEqual(self.db.query(self.m.Account).count(), 2)
        _merge_masked_accounts(self.db)
        self.assertEqual(self.db.query(self.m.Account).count(), 1)
        self.assertEqual(self.db.query(self.m.Balance).count(), 2)           # both statements' balances kept

    def test_maintenance_does_not_merge_the_proven_second_row_away(self):
        from app.etl.maintenance import _dedup_transactions
        self.store(row("1744.77"), row("1531.77"))
        _dedup_transactions(self.db)
        self.assertEqual(self.count(), 2)


if __name__ == "__main__":
    unittest.main()
