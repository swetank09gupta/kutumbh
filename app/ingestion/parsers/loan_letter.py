"""
HDFC home-loan part-prepayment confirmation ("We are in receipt of Rs. N towards Part prepayment ...").

The letter states the principal still owed after the prepayment, the revised EMI, rate and the
number of EMIs left — the lender's own figure, so it replaces any hand-kept outstanding balance.
Enquiry letters ("we refer to your enquiry ... intention to partly prepay") are quotes, not events,
and are ignored.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Optional


@dataclass
class LoanLetter:
    loan_no: str
    as_on: date
    prepaid: Decimal
    principal_after: Decimal
    emi: Optional[Decimal]
    rate: Optional[Decimal]
    emis_left: Optional[int]


_LOAN = re.compile(r"Loan Account No\s*:\s*(\d{6,})", re.I)
_RECEIPT = re.compile(r"We are in receipt of Rs\.?\s*([\d,.]+) towards Part prepayment", re.I)
_AFTER = re.compile(r"principal amount outstanding against your loan after a principal prepayment of Rs\.?\s*([\d,.]+)"
                    r"\s+as on\s+(\d{2}-[A-Za-z]{3}-\d{4})\s+will be Rs\.?\s*([\d,.]+)", re.I)
_EMI = re.compile(r"REVISED EMI \(Rs\.\)\s*:\s*([\d,.]+)\s*@\s*([\d.]+)\s+NUMBER OF EMIs\s*:\s*(\d+)", re.I)


def parse_letter(text: str) -> Optional[LoanLetter]:
    text = re.sub(r"\s+", " ", text or "")
    if not _RECEIPT.search(text):
        return None
    loan, after = _LOAN.search(text), _AFTER.search(text)
    if not loan or not after:
        return None
    emi = _EMI.search(text)
    d = lambda s: Decimal(s.replace(",", ""))
    return LoanLetter(
        loan_no=loan.group(1), as_on=datetime.strptime(after.group(2).title(), "%d-%b-%Y").date(),
        prepaid=d(after.group(1)), principal_after=d(after.group(3)),
        emi=d(emi.group(1)) if emi else None, rate=d(emi.group(2)) if emi else None,
        emis_left=int(emi.group(3)) if emi else None)
