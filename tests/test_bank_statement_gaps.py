"""
Three ways a real bank row went missing (Oct 2026), each pinned here with its own test:
  * HDFC statement whose first printed day has no opening balance: the first row's amount is in the Withdrawal/Deposit column
    and was skipped (a ₹1.8L own-account credit);
  * a UPI reversal credit shares date and reference with the debit it undoes: the reference-only identity dropped it;
  * Axis statements came from a new domain (axis.bank.in) and carried no account number (everything landed on "unknown").
"""
import unittest
from datetime import date
from decimal import Decimal as D

from app.etl.dedup import transaction_fingerprint
from app.ingestion.email_fetcher import _match_sender
from app.ingestion.parsers.axis import AxisParser
from app.ingestion.parsers.hdfc import HdfcParser


class HdfcFirstRow(unittest.TestCase):
    """Stacked-table row cells: [dates, narration, ref, valuedate, withdrawals, deposits, balances]."""
    kind = staticmethod(lambda wd, dep, bal: HdfcParser()._first_row_kind(
        ["d", "n", "r", "v", wd, dep, bal], 4, 5, [D(b) for b in bal.split("\n")]))

    def test_first_row_a_credit(self):          # the later two rows are debits of 221 and 42; the deposit column has one more amount
        self.assertEqual(self.kind("221.00\n42.00", "180,000.00", "192259.94\n192038.94\n191996.94"), ("credit", D("180000.00")))

    def test_first_row_a_debit(self):
        self.assertEqual(self.kind("500.00\n221.00", "42.00", "10000.00\n10042.00\n9821.00"), ("debit", D("500.00")))

    def test_a_single_row(self):
        self.assertEqual(self.kind("", "75.00", "1075.00"), ("credit", D("75.00")))

    def test_a_column_that_does_not_line_up_gives_nothing(self):
        self.assertIsNone(self.kind("221.00\n43.00", "180,000.00", "192259.94\n192038.94\n191996.94"))   # 43 is not the delta 42

    def test_both_columns_fitting_is_not_proof(self):
        self.assertIsNone(self.kind("5.00", "5.00", "100.00"))


class ReversalKeepsItsOwnIdentity(unittest.TestCase):
    def fp(self, kind, amount="50000.00", ref="0000102738290", bank=True):
        return transaction_fingerprint("acc", date(2025, 4, 7), D(amount), kind, "UPI-ZERODHA", ref, with_direction=bank)

    def test_a_reversal_credit_is_not_the_debit_it_undoes(self):
        self.assertNotEqual(self.fp("debit"), self.fp("credit"))

    def test_a_different_amount_on_the_same_reference_is_another_row(self):
        self.assertNotEqual(self.fp("debit"), self.fp("debit", "49999.00"))

    def test_broker_rows_keep_the_reference_only_key_the_ledger_links_to(self):
        # The ledger's trades point at their cashflow row by this fingerprint: changing it orphaned 216 trades (Oct 2026).
        self.assertEqual(self.fp("debit", bank=False), self.fp("credit", "1.00", bank=False))
        self.assertEqual(self.fp("debit", bank=False),
                         __import__("hashlib").sha256(f"acc|{date(2025, 4, 7)}|102738290".encode()).hexdigest())

    def test_the_same_payment_in_two_statements_is_still_one_row(self):
        self.assertEqual(self.fp("debit", "50000"), self.fp("debit", "50000.00", "102738290"))   # padding and scale do not matter


class AxisStatements(unittest.TestCase):
    def test_the_new_sender_is_read_but_payment_advices_are_not(self):
        self.assertEqual(_match_sender("AXIS BANK <statements@axis.bank.in>")["institution"], "Axis")
        self.assertIsNone(_match_sender("Straight2axis@axis.bank.in"))

    def test_account_number_from_the_statement(self):
        ref = AxisParser._account_ref
        self.assertEqual(ref("Detailed Statement for a/c no. XXXXXXXXXXX3856 between 01-04-2020 to 30-04-2020"), "3856")
        self.assertEqual(ref("Statement(cid:9)for(cid:9)Account(cid:9)No.(cid:9)50410XXXXX(cid:9)for the period"), "50410XXXXX")
        self.assertEqual(ref("There have been no customer induced transactions in your account XXXXXX3237 for the last"), None)  # prose, not a header
        self.assertEqual(ref("Account Number : XXXXXX3237 3\nSr.No."), "XXXXXX3237")        # short masks keep their width
        self.assertIsNone(ref("Account No. Branch IFSC Code"))


class MaskedAccountNumbers(unittest.TestCase):
    def test_two_masks_of_one_number_agree(self):
        from app.etl.maintenance import _masks_agree
        self.assertTrue(_masks_agree("50410XXXXX", "XXXXXX3237"))           # the ex-Citi account, two statement layouts
        self.assertFalse(_masks_agree("50410XXXXX", "XXXXXX3237X"))         # different width: not the same number
        self.assertFalse(_masks_agree("50410XXXXX", "51410XXXXX"))          # a visible digit differs
        self.assertFalse(_masks_agree("3856", "3856"))                      # nothing masked: exact refs are matched elsewhere


class ParseCacheIsPerParser(unittest.TestCase):
    """Bumping one parser's version must not throw away every other parser's cached results (a cold re-parse took hours, Oct 2026)."""

    def test_a_parser_version_changes_only_that_parsers_cache_key(self):
        import os, tempfile
        from pathlib import Path
        from app.ingestion.parsers.base import BaseParser, ParseResult
        calls = []

        class A(BaseParser):
            def parse(self, path): calls.append("A"); return ParseResult(institution="A", account_type="bank")

        class B(BaseParser):
            def parse(self, path): calls.append("B"); return ParseResult(institution="B", account_type="bank")

        with tempfile.TemporaryDirectory() as d, mock_env(PARSE_RESULT_CACHE_DIR=d):
            f = Path(d) / "x.txt"; f.write_text("statement")
            A().parse_cached(f); B().parse_cached(f); A().parse_cached(f); B().parse_cached(f)
            self.assertEqual(calls, ["A", "B"])                                 # second reads hit the cache
            A._PARSER_VERSION = "+a2"                                           # the parser that changed
            try:
                A().parse_cached(f); B().parse_cached(f)
            finally:
                del A._PARSER_VERSION
            self.assertEqual(calls, ["A", "B", "A"])                            # only A re-parsed; B still cached

    def test_the_axis_key_is_unchanged_by_the_move_to_a_suffix(self):
        self.assertEqual(AxisParser._CACHE_SCHEMA + AxisParser._PARSER_VERSION, "v18+axis2")


def mock_env(**kv):
    import os
    from unittest import mock
    return mock.patch.dict(os.environ, kv)


if __name__ == "__main__":
    unittest.main()
