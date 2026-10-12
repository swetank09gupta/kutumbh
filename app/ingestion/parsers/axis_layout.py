"""
Axis Bank statement tables, read by column position.

The monthly PDF prints the account's transactions between "Opening Balance" and "Closing Balance" in columns
(Withdrawals | Deposits | Balance). Text extraction loses which column a number sat in, and the same pages also carry a
"relationship summary" and "summary of deposits" whose numbers (a fixed deposit of INR 100,000.00) look like amounts. So:

  * the table is found by its header row, its columns by the header words' x positions;
  * a number is a withdrawal, a deposit or a balance by the header it sits under, never by guesswork;
  * the block is accepted only if  opening + deposits - withdrawals  equals the closing balance the statement prints;
  * term-deposit blocks (header with "Value Date") are read separately from savings.

Pure functions over pdfplumber words, so they can be tested without a PDF.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

D = Decimal
_DATE = re.compile(r"^\d{2}-\d{2}-\d{4}$")
_NUM = re.compile(r"^[\d,]+\.\d{2}$")


@dataclass
class Row:
    day: date
    narration: str
    kind: str                      # debit | credit
    amount: Decimal
    balance: Optional[Decimal] = None


@dataclass
class Block:
    kind: str                      # savings | deposit
    opening: Optional[Decimal] = None
    closing: Optional[Decimal] = None
    rows: list[Row] = field(default_factory=list)
    reconciles: bool = False

    def check(self) -> bool:
        if self.opening is None or self.closing is None:
            return False
        net = sum((r.amount if r.kind == "credit" else -r.amount for r in self.rows), D(0))
        self.reconciles = abs(self.opening + net - self.closing) <= D("0.01")
        return self.reconciles


def _money(s: str) -> Decimal:
    return D(s.replace(",", ""))


def _lines(words: list[dict]) -> list[list[dict]]:
    out: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if out and abs(out[-1][0]["top"] - w["top"]) <= 2.5:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(l, key=lambda w: w["x0"]) for l in out]


def _header_anchors(line: list[dict]) -> Optional[dict]:
    texts = {w["text"]: w for w in line}
    if "Withdrawals" in texts and "Deposits" in texts and "Balance" in texts and "Txn" in texts:
        return {"wd": texts["Withdrawals"], "dep": texts["Deposits"], "bal": texts["Balance"],
                "kind": "deposit" if "Value" in texts else "savings"}
    return None


def read_blocks(pages_words: list[list[dict]]) -> list[Block]:
    """All transaction tables in a statement (savings and term-deposit ledgers), each with its own reconciliation."""
    blocks: list[Block] = []
    cur: Optional[Block] = None
    anchors: Optional[dict] = None
    for words in pages_words:
        for line in _lines(words):
            a = _header_anchors(line)
            if a:
                anchors = a
                if cur is None or cur.kind != a["kind"] or cur.closing is not None:
                    cur = Block(kind=a["kind"])
                    blocks.append(cur)
                continue
            if cur is None or anchors is None:
                continue
            text = " ".join(w["text"] for w in line)
            if text.startswith("Opening Balance"):
                nums = [w for w in line if _NUM.match(w["text"]) or re.match(r"^[\d,]+(\.\d+)?$", w["text"])]
                if nums and cur.opening is None:
                    cur.opening = _money(nums[-1]["text"])
                continue
            if text.startswith("Closing Balance"):
                nums = [w for w in line if re.match(r"^[\d,]+(\.\d+)?$", w["text"])]
                if nums:
                    cur.closing = _money(nums[-1]["text"])
                cur.check()
                cur = None
                continue
            first = line[0]
            wd_left = anchors["wd"]["x0"] - 8
            narr_words = [w for w in line if w["x1"] < wd_left + 6 and not (w is first and _DATE.match(w["text"]))]
            nums = [w for w in line if _NUM.match(w["text"]) and w["x0"] >= wd_left]
            if _DATE.match(first["text"]) and first["x0"] < anchors["wd"]["x0"] - 120:
                row = Row(day=datetime.strptime(first["text"], "%d-%m-%Y").date(),
                          narration=" ".join(w["text"] for w in narr_words), kind="debit", amount=D(0))
                for w in nums:
                    col = min(("wd", "dep", "bal"), key=lambda c: abs(w["x1"] - anchors[c]["x1"]))
                    if col == "bal":
                        row.balance = _money(w["text"])
                    else:
                        row.kind, row.amount = ("debit" if col == "wd" else "credit"), _money(w["text"])
                cur.rows.append(row)
            elif cur.rows and narr_words and not nums:                 # a wrapped narration continues the row above
                cur.rows[-1].narration += " " + " ".join(w["text"] for w in narr_words)
            elif cur.rows and nums and not _DATE.match(first["text"]):
                # a wrapped row whose amount landed on the continuation line
                last = cur.rows[-1]
                if last.amount == 0:
                    for w in nums:
                        col = min(("wd", "dep", "bal"), key=lambda c: abs(w["x1"] - anchors[c]["x1"]))
                        if col == "bal":
                            last.balance = _money(w["text"])
                        else:
                            last.kind, last.amount = ("debit" if col == "wd" else "credit"), _money(w["text"])
                last.narration += " " + " ".join(w["text"] for w in narr_words)
    return blocks


_TD_HEAD = re.compile(r"Term Deposit No\.\s+X*(\d+)\s+-\s+Quick View")
_TD_SUMMARY = re.compile(r"X*(\d{3,})\s+.*?\s(\d+(?:\.\d+)?)\s+INR\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+(\d{2}-\d{2}-\d{4})\s+\S+\s+(\d{2}-\d{2}-\d{4})")


@dataclass
class TermDeposit:
    number_tail: str
    principal: Decimal
    maturity_value: Decimal
    rate: Decimal
    start: date
    maturity: date
    kind: str                       # REINVESTMENT DEPOSIT | RECURRING ... as the bank names it


def read_term_deposits(text: str) -> list[TermDeposit]:
    """Deposits listed in 'Summary of Deposits': number, amount, maturity value, rate, start and end dates."""
    out = []
    for m in _TD_SUMMARY.finditer(re.sub(r"[ \t]+", " ", text)):
        tail, rate, principal, maturity_value, start, end = m.groups()
        out.append(TermDeposit(tail[-4:], _money(principal), _money(maturity_value), D(rate),
                               datetime.strptime(start, "%d-%m-%Y").date(), datetime.strptime(end, "%d-%m-%Y").date(), ""))
    return out
