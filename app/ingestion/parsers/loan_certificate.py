"""
HDFC home-loan tax documents (IT_FINAL / IT_PROV): the lender's own figures for one financial year.

  "Certificate for interest - 01/04/2024 - 31/03/2025"                       (final, issued in April)
  "PROVISIONAL STATEMENT for claiming deduction under Sections 24(b) & 80C ... 01/04/2025 to 31/03/2026"

Both print the principal and interest components of the year's EMIs (and pre-EMI interest in a construction year). A
provisional statement is issued in advance and "subject to change in case of prepayment/s", so it is never used to
override a computed year; a final certificate is the authority for the interest claim. HDFC's own note says principal
repaid "through EMIs and/or Prepayments" qualifies under 80C.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

_NUM = r"([\d,]+(?:\.\d+)?)"


@dataclass
class LoanCertificate:
    kind: str                        # final | provisional
    period_from: date
    period_to: date
    doc_date: Optional[date]
    principal: Decimal
    interest: Decimal
    pre_emi_interest: Decimal
    emi_total: Optional[Decimal]
    rate: Optional[Decimal]

    @property
    def fy(self) -> str:
        return f"{self.period_from.year}-{str(self.period_from.year + 1)[2:]}"


def _d(s: str) -> date:
    return datetime.strptime(s, "%d/%m/%Y").date()


def _m(s: Optional[str]) -> Decimal:
    return Decimal(s.replace(",", "")) if s else Decimal(0)


def parse_certificate(text: str) -> Optional[LoanCertificate]:
    """None for anything that is not one of the two documents (the other `IT_` PDFs are different mails)."""
    t = re.sub(r"[ \t]+", " ", text)
    final = re.search(r"Certificate for interest\s*-\s*(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})", t, re.I)
    prov = re.search(r"PROVISIONAL STATEMENT.*?for the period\s*(\d{2}/\d{2}/\d{4})\s*to\s*(\d{2}/\d{2}/\d{4})", t, re.I | re.S)
    hit, kind = (final, "final") if final else (prov, "provisional") if prov else (None, "")
    if not hit:
        return None
    pr = re.search(r"PRINCIPAL COMPONENT Rs\.\s*" + _NUM, t, re.I)
    it = re.search(r"INTEREST COMPONENT Rs\.\s*" + _NUM, t, re.I)
    if not pr or not it:
        return None
    doc = re.search(r"Date\s*:\s*(\d{2}/\d{2}/\d{4})", t)
    emi = re.search(r"EMIs payable from [\d/]+ to [\d/]+ is Rs\.\s*" + _NUM, t, re.I)
    rate = re.search(r"@\s*([\d.]+)%", t)
    pre = re.search(r"PRE-EMI INTEREST Rs\.\s*" + _NUM, t, re.I)
    return LoanCertificate(kind, _d(hit.group(1)), _d(hit.group(2)), _d(doc.group(1)) if doc else None, _m(pr.group(1)), _m(it.group(1)),
                           _m(pre.group(1) if pre else None), _m(emi.group(1)) if emi else None, Decimal(rate.group(1)) if rate else None)
