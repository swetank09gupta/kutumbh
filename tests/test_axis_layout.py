"""
Axis statements read by column position, and only if they reconcile. The fixtures reproduce the real layout (column
positions of Withdrawals / Deposits / Balance) and the traps that corrupted the wife's account for years: a relationship
summary line "Fixed Deposit INR 100,000.00", term-deposit ledgers, wrapped narrations, rows with no printed balance.
"""
import unittest
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from app.ingestion.parsers import axis_layout as al
from app.ingestion.parsers.axis import AxisParser

WD = dict(x0=285.6, x1=332.6)
DEP = dict(x0=361.1, x1=393.1)
BAL = dict(x0=435.6, x1=463.6)


def w(text, x0, x1, top):
    return {"text": text, "x0": x0, "x1": x1, "top": top}


class Page:
    """A fake pdfplumber page: lines of words at increasing y."""
    def __init__(self):
        self.words, self.y = [], 100.0
        self.text_lines = []

    def line(self, *words, text=None):
        self.y += 12
        for t, x0, x1 in words:
            self.words.append(w(t, x0, x1, self.y))
        self.text_lines.append(text if text is not None else " ".join(t for t, _, _ in words))
        return self

    def header(self, deposit_ledger=False):
        cols = [("Txn", 33.1, 47.9), ("Date", 50, 70), ("Transaction", 155.6, 199.6)]
        if deposit_ledger:
            cols += [("Value", 240, 262), ("Date", 264, 284)]
        cols += [("Withdrawals", WD["x0"], WD["x1"]), ("Deposits", DEP["x0"], DEP["x1"]), ("Balance", BAL["x0"], BAL["x1"])]
        return self.line(*cols)

    def opening(self, amount):
        return self.line(("Opening Balance", 33, 100), (amount, BAL["x0"], BAL["x1"]))

    def closing(self, amount):
        return self.line(("Closing Balance", 33, 100), (amount, BAL["x0"], BAL["x1"]))

    def txn(self, day, narration, amount, col, balance=None):
        cols = WD if col == "wd" else DEP
        words = [(day, 33.1, 80.0), *[(t, 90 + 20 * i, 105 + 20 * i) for i, t in enumerate(narration.split())], (amount, cols["x0"], cols["x1"])]
        if balance:
            words.append((balance, BAL["x0"], BAL["x1"]))
        return self.line(*words)

    def wrap(self, narration):
        return self.line(*[(t, 90 + 20 * i, 105 + 20 * i) for i, t in enumerate(narration.split())])

    def extract_words(self):
        return self.words

    def extract_text(self):
        return "\n".join(self.text_lines)


def statement():
    p = Page()
    p.line(("Relationship", 30, 90), ("summary", 92, 130))
    p.line(("Fixed", 30, 50), ("Deposit", 52, 80), ("INR", 90, 100), ("100,000.00", 105, 150),
           text="Fixed Deposit INR 100,000.00 Overdraft Account/s INR 0.00")                       # the trap: looks like an amount
    p.header()
    p.opening("1,000.00")
    p.txn("02-10-2025", "IMPS/P2A/527521626538/Akanksha", "30,000.00", "dep")
    p.wrap("DepositI/9195820151019002000")
    p.txn("04-10-2025", "MOB-TD/925040101745385/AKANKSHA", "20,000.00", "wd")
    p.txn("09-10-2025", "IO For 919040045185859", "605.00", "dep", balance="11,605.00")
    p.closing("11,605.00")
    return p


class Layout(unittest.TestCase):
    def test_a_number_is_a_withdrawal_or_a_deposit_by_the_column_it_sits_under(self):
        (b,) = al.read_blocks([statement().extract_words()])
        self.assertEqual([(r.kind, r.amount) for r in b.rows], [("credit", D("30000.00")), ("debit", D("20000.00")), ("credit", D("605.00"))])
        self.assertEqual(b.rows[2].balance, D("11605.00"))
        self.assertIsNone(b.rows[0].balance)                                   # newer statements print no per-row balance

    def test_the_block_reconciles_to_the_printed_balances(self):
        (b,) = al.read_blocks([statement().extract_words()])
        self.assertEqual((b.opening, b.closing, b.reconciles), (D("1000.00"), D("11605.00"), True))     # 1000 + 30000 - 20000 + 605

    def test_a_wrapped_narration_joins_the_row_above(self):
        (b,) = al.read_blocks([statement().extract_words()])
        self.assertIn("DepositI/9195820151019002000", b.rows[0].narration)

    def test_the_relationship_summary_is_never_a_transaction(self):               # the shipped bug: 100,000.00 rows
        (b,) = al.read_blocks([statement().extract_words()])
        self.assertNotIn(D("100000.00"), [r.amount for r in b.rows])
        self.assertEqual(len(b.rows), 3)

    def test_a_wrong_closing_balance_does_not_reconcile(self):
        p = statement(); p.words = [dict(x, text="99,999.00") if x["text"] == "11,605.00" and x["x0"] == BAL["x0"] and x["top"] == p.words[-1]["top"] else x for x in p.words]
        (b,) = al.read_blocks([p.extract_words()])
        self.assertFalse(b.reconciles)

    def test_a_term_deposit_ledger_is_a_separate_block_and_does_not_pollute_savings(self):
        p = statement()
        p.header(deposit_ledger=True); p.opening("0.00")
        p.txn("04-10-2025", "ICONN/M26431", "100,000.00", "dep")
        p.closing("100,000.00")
        blocks = al.read_blocks([p.extract_words()])
        self.assertEqual([b.kind for b in blocks], ["savings", "deposit"])
        self.assertTrue(all(b.reconciles for b in blocks))
        self.assertEqual(len(blocks[0].rows), 3)

    def test_a_table_continuing_on_the_next_page_without_a_header_is_still_read(self):
        a = Page(); a.header(); a.opening("0.00"); a.txn("01-10-2025", "UPI/one", "100.00", "dep")
        b = Page(); b.txn("02-10-2025", "UPI/two", "40.00", "wd"); b.closing("60.00")
        (blk,) = al.read_blocks([a.extract_words(), b.extract_words()])
        self.assertEqual((len(blk.rows), blk.reconciles), (2, True))


TD_LINE = ("Summary of Deposits as on 31-10-2025\nDeposit No. Base Branch CRN Deposit Maturity Value Start Date End Date\n"
           "XXXXXXXXXXX5385 DLF CYBER CITY HR ARIC 6.6 INR 100,000.00 110,308.00 04-10-2025 18/0 04-04-2027\n"
           "XXXXXXXXXXX5859 RAMPUR [UP] MIC INR 100,000.0 09-06-2019 24/0 09-06-2021 0\n")


class Deposits(unittest.TestCase):
    def test_a_running_term_deposit_is_read_with_amount_rate_and_dates(self):
        (d,) = al.read_term_deposits(TD_LINE)
        self.assertEqual((d.number_tail, d.principal, d.maturity_value, d.rate, d.start, d.maturity),
                         ("5385", D("100000.00"), D("110308.00"), D("6.6"), date(2025, 10, 4), date(2027, 4, 4)))

    def test_an_older_garbled_layout_is_ignored_not_guessed(self):
        self.assertEqual(len(al.read_term_deposits(TD_LINE)), 1)


class FakePdf:
    def __init__(self, pages): self.pages = pages
    def __enter__(self): return self
    def __exit__(self, *a): return False


class ParserEndToEnd(unittest.TestCase):
    def parse(self, page, tail=""):
        full = Page()
        full.words, full.text_lines = page.words, page.text_lines + [
            "Detailed Statement for a/c no. XXXXXXXXXXX3856 between 01-10-2025 to 31-10-2025", *TD_LINE.splitlines()]
        p = AxisParser()
        with mock.patch.object(AxisParser, "_open_pdf", return_value=FakePdf([full])):
            return p._parse_pdf(Path("stmt.pdf"))

    def test_transactions_balance_statement_date_and_the_running_deposit(self):
        r = self.parse(statement())
        self.assertEqual([(t.transaction_type, t.amount) for t in r.transactions],
                         [("credit", D("30000.00")), ("debit", D("20000.00")), ("credit", D("605.00"))])
        self.assertEqual((r.balance.balance, r.statement_date), (D("11605.00"), date(2025, 10, 31)))
        (fd,) = r.manual_assets
        self.assertEqual((fd.name, fd.asset_type, fd.current_value, fd.interest_rate, fd.maturity_amount),
                         ("Axis FD ...5385", "fd", D("100000.00"), D("6.6"), D("110308.00")))
        self.assertEqual(r.parse_warnings, [])

    def test_a_statement_that_does_not_reconcile_stores_nothing(self):
        p = statement()
        for x in p.words:
            if x["text"] == "1,000.00":
                x["text"] = "9,000.00"                                           # opening balance misread
        r = self.parse(p)
        self.assertEqual((r.transactions, r.manual_assets, r.balance), ([], [], None))
        self.assertIn("does not reconcile", r.parse_warnings[0])

    def test_an_ended_deposit_is_not_listed_as_running(self):
        p = statement()
        full = Page(); full.words = p.words
        full.text_lines = p.text_lines + ["Detailed Statement for a/c no. X between 01-05-2027 to 31-05-2027", *TD_LINE.splitlines()]
        with mock.patch.object(AxisParser, "_open_pdf", return_value=FakePdf([full])):
            r = AxisParser()._parse_pdf(Path("s.pdf"))
        self.assertEqual(r.manual_assets, [])                                    # matured 4 Apr 2027, statement of May 2027


class DepositLivenessIsPerBank(unittest.TestCase):
    class A:
        def __init__(self, owner, name): self.owner_id, self.name, self.asset_type = owner, name, "fd"

    def test_an_axis_deposit_is_judged_against_axis_statements_not_sbi_s_newer_ones(self):
        from app.engine.deposits import is_live
        series = {("wife", "Axis FD ...5385"): [(date(2025, 12, 31), D(100000), None)],
                  ("wife", "SBI FD ...1111"): [(date(2026, 8, 31), D(1), None)]}
        newest = {("wife", "Axis"): date(2025, 12, 31), ("wife", "SBI"): date(2026, 8, 31)}
        self.assertTrue(is_live(self.A("wife", "Axis FD ...5385"), series, newest))           # listed on the newest Axis table
        series[("wife", "Axis FD ...9999")] = [(date(2025, 6, 30), D(5), None)]
        self.assertFalse(is_live(self.A("wife", "Axis FD ...9999"), series, newest))          # gone from the newest Axis table

    def test_bank_is_the_first_word_of_the_name(self):
        from app.engine.deposits import bank_of
        self.assertEqual((bank_of("SBI FD ...6925"), bank_of("Axis FD ...5385"), bank_of(None)), ("SBI", "Axis", ""))


if __name__ == "__main__":
    unittest.main()


class AxisDepositGrowthStartsAtTheOpenDate(unittest.TestCase):
    """The statement prints the opening principal and no accrued column: base_value() is true on the OPEN date, not the statement's."""
    def asset(self, **kw):
        base = dict(asset_type="fd", current_value=D(100000), accrued_interest=None, maturity_date=date(2027, 4, 4),
                    source_statement_date=date(2025, 12, 31), notes="Maturity value: ₹110,308.00. Principal as of 2025-10-04.")
        base.update(kw)
        return mock.Mock(**base)

    def test_the_note_carries_the_principal_date(self):
        from app.etl.normalizer import _deposit_notes
        pa = mock.Mock(maturity_amount=D("110308.00"), principal_as_of=date(2025, 10, 4))
        self.assertEqual(_deposit_notes(pa), "Maturity value: ₹110,308.00. Principal as of 2025-10-04.")
        self.assertEqual(_deposit_notes(mock.Mock(maturity_amount=D("110308.00"), principal_as_of=None)), "Maturity value: ₹110,308.00")

    def test_the_maturity_value_is_still_read_from_the_longer_note(self):
        from app.engine.deposits import maturity_value
        self.assertEqual(maturity_value(self.asset()), D("110308.00"))

    def test_value_follows_the_contract_curve_from_the_open_date(self):
        from app.engine.deposits import accrued_value
        v = accrued_value(self.asset(), date(2026, 10, 12))
        expected = 100000 * (110308 / 100000) ** ((date(2026, 10, 12) - date(2025, 10, 4)).days / (date(2027, 4, 4) - date(2025, 10, 4)).days)
        self.assertAlmostEqual(float(v), expected, delta=0.01)
        self.assertGreater(float(v), 106800)                                      # not the ~106,300 the statement-date anchor gave

    def test_the_implied_rate_is_the_deposits_real_one_not_8_percent(self):
        from app.engine.deposits import contract_rate_pct
        self.assertAlmostEqual(contract_rate_pct(self.asset()), 6.76, delta=0.05)

    def test_an_sbi_style_asset_with_accrued_interest_keeps_the_statement_date_anchor(self):
        from app.engine.deposits import _anchor_date
        a = self.asset(accrued_interest=D(500), notes="Maturity value: ₹110,308.00")
        self.assertEqual(_anchor_date(a), date(2025, 12, 31))


class DormantAccount(unittest.TestCase):
    TEXT = ("Statement(cid:9)for(cid:9)Account(cid:9)No.(cid:9)50410XXXXX(cid:9)for(cid:9)the(cid:9)period(cid:9)from(cid:9)01-07-2025(cid:9)to(cid:9)31-07-2025\n"
            "Date Transaction Details Chq No. Withdrawal Deposits Balance\nOpening Balance 0.00\nClosing Balance 0.00\nTotal 0.00 0.00\n")

    def parse(self, text):
        page = mock.Mock(); page.extract_words.return_value = []; page.extract_text.return_value = text
        pdf = mock.MagicMock(); pdf.__enter__.return_value = pdf; pdf.pages = [page]
        with mock.patch.object(AxisParser, "_open_pdf", return_value=pdf):
            return AxisParser()._parse_pdf(Path("s.pdf"))

    def test_a_statement_with_no_transactions_and_equal_balances_is_reconciled_not_a_warning(self):
        r = self.parse(self.TEXT)
        self.assertEqual((r.parse_warnings, r.transactions, r.balance.balance, r.balance.snapshot_date), ([], [], D(0), date(2025, 7, 31)))

    def test_balances_that_differ_with_no_rows_still_do_not_pass(self):
        r = self.parse(self.TEXT.replace("Closing Balance 0.00", "Closing Balance 500.00"))
        self.assertTrue(r.parse_warnings)

    def test_a_dated_row_means_it_is_not_an_empty_statement(self):
        r = self.parse(self.TEXT.replace("Opening Balance 0.00", "Opening Balance 0.00\n05-07-2025 UPI 100.00 0.00 0.00"))
        self.assertTrue(r.parse_warnings)

