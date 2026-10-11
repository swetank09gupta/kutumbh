"""
CDSL "Transactions In Your Demat Account" alert parser.

Body-only e-mail, one row per ISIN movement:

    Dear MISS AKANKSHA, Following is/are the list of transactions for your Demat account
    ending with *08489463 Sr. No. Company Name ISIN Quantity Debit / Credit Date and Time
    1 MOTILAL OSWAL AMC LTD#MOTILAL OSWAL MF-... INF247L01999 335.064 Credit 05/10/2026 19:57:42 ...

Quantity-only evidence (no price, no amount): it never becomes a cashflow, it is stored as a
demat movement the ledger uses to see units credited after the last monthly statement.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from app.ingestion.parsers.base import BaseParser, ParsedTransaction, ParseResult

logger = logging.getLogger(__name__)

_SUFFIX_RE = re.compile(r"ending\s+with\s+\*?(\d{4,})", re.I)
_ROW_RE = re.compile(
    r"\b\d+\s+(?P<name>.+?)\s+(?P<isin>IN[A-Z0-9]{10})\s+(?P<qty>[\d,]+(?:\.\d+)?)\s+"
    r"(?P<dir>Credit|Debit)\s+(?P<d>\d{2}/\d{2}/\d{4})\s+(?P<t>\d{2}:\d{2}:\d{2})",
    re.I,
)


class CdslAlertParser(BaseParser):
    institution  = "CDSL"           # stored under the owner's CDSL demat account
    account_type = "demat"

    def parse(self, path: Path) -> ParseResult:
        html = path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"<style.*?</style>|<script.*?</script>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;|&#160;", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return ParseResult(institution=self.institution, account_type=self.account_type,
                           transactions=self.parse_text(text), source_file=str(path))

    def parse_text(self, text: str) -> list[ParsedTransaction]:
        sm = _SUFFIX_RE.search(text)
        suffix = sm.group(1)[-8:] if sm else ""
        out: list[ParsedTransaction] = []
        for m in _ROW_RE.finditer(text):
            qty = self._to_decimal(m.group("qty"))
            when = datetime.strptime(f"{m.group('d')} {m.group('t')}", "%d/%m/%Y %H:%M:%S")
            if not qty or qty <= 0:
                continue
            credit = m.group("dir").lower() == "credit"
            name = m.group("name").split("#")[-1].strip()
            out.append(ParsedTransaction(
                date=when.date(), amount=qty, transaction_type="credit" if credit else "debit",
                description=f"CDSL ALERT {m.group('dir').upper()} {qty} {name}"[:200],
                # keeps the demat suffix and the time: they make the movement's identity unique
                raw_description=f"CDSL alert *{suffix} {when:%H:%M:%S} {name}"[:300],
                counterparty=name, isin=m.group("isin"),
                quantity=qty, kind="cdsl_alert",
            ))
        return out
