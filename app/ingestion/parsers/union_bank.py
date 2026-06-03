"""
Union Bank of India statement parser — CSV and PDF.

CSV columns (common format): Date | Particulars | Debit | Credit | Balance
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pandas as pd
from app.ingestion.parsers.base import (
    BaseParser, ParsedBalance, ParsedTransaction, ParseResult,
)

logger = logging.getLogger(__name__)

DATE_FMTS = ["%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d-%b-%Y", "%d/%m/%y"]


class UnionBankParser(BaseParser):
    institution  = "Union Bank"
    account_type = "bank"

    def parse(self, path: Path) -> ParseResult:
        if path.suffix.lower() == ".pdf":
            return self._parse_pdf(path)
        return self._parse_csv(path)

    def _parse_csv(self, path: Path) -> ParseResult:
        raw = path.read_text(errors="ignore")
        lines = raw.splitlines()

        header_idx = 0
        for i, line in enumerate(lines):
            low = line.lower()
            if "date" in low and ("particulars" in low or "description" in low):
                header_idx = i
                break

        account_ref = None
        for line in lines[:header_idx]:
            m = re.search(r"(\d{10,16})", line)
            if m:
                account_ref = m.group(1)[-4:]
                break

        try:
            df = pd.read_csv(path, skiprows=header_idx, on_bad_lines="skip")
        except Exception as exc:
            logger.error("Union Bank CSV parse error %s: %s", path.name, exc)
            return ParseResult(institution=self.institution, account_type=self.account_type)

        df.columns = [c.strip() for c in df.columns]
        transactions: list[ParsedTransaction] = []
        last_balance = None

        for _, row in df.iterrows():
            date_str = str(row.get("Date", row.get("Txn Date", ""))).strip()
            date_val = self._parse_date(date_str, DATE_FMTS)
            if date_val is None:
                continue

            desc   = str(row.get("Particulars", row.get("Description", ""))).strip()
            debit  = self._to_decimal(row.get("Debit", row.get("DR", "")))
            credit = self._to_decimal(row.get("Credit", row.get("CR", "")))
            bal    = self._to_decimal(row.get("Balance", row.get("Bal", "")))

            if debit and debit > 0:
                txn_type, amount = "debit", debit
            elif credit and credit > 0:
                txn_type, amount = "credit", credit
            else:
                continue

            if bal:
                last_balance = bal

            transactions.append(ParsedTransaction(
                date=date_val, amount=amount, transaction_type=txn_type,
                description=desc, raw_description=desc, balance_after=bal,
            ))

        balance = ParsedBalance(balance=last_balance, snapshot_date=transactions[-1].date) \
            if transactions and last_balance else None

        return ParseResult(
            institution=self.institution, account_type=self.account_type,
            account_ref=account_ref, transactions=transactions,
            balance=balance, source_file=str(path),
        )

    @staticmethod
    def _strip_cr_dr(value: str) -> str:
        """Remove trailing ' Cr' / ' Dr' from Union Bank balance strings."""
        return re.sub(r"\s*[CcDd][Rr]\s*$", "", str(value or "")).strip()

    def _parse_pdf(self, path: Path) -> ParseResult:
        """
        Union Bank emailed PDF table layout:
          [SI, Date, Particulars, Chq Num, Withdrawal, Deposit, Balance]

        Balance cells look like '12,037.62 Cr' — strip the Cr/Dr suffix.
        Account number is in the page header: 'Account Number : 3527XXXXXXX6026'
        """
        transactions: list[ParsedTransaction] = []
        last_balance = None
        account_ref  = None

        with self._open_pdf(path) as pdf:
            for page in pdf.pages:
                # Extract account ref from page text
                if account_ref is None:
                    text = page.extract_text() or ""
                    m = re.search(r"Account Number\s*:\s*[\dX*]+(\d{4})", text)
                    if m:
                        account_ref = m.group(1)

                for table in (page.extract_tables() or []):
                    for row in table:
                        if not row or len(row) < 6:
                            continue
                        # row[0] = SI (serial no.) — use row[1] for date
                        date_val = self._parse_date(str(row[1] or ""), DATE_FMTS)
                        if date_val is None:
                            continue

                        desc   = str(row[2] or "").strip().replace("\n", " ")
                        debit  = self._to_decimal(row[4])
                        credit = self._to_decimal(row[5]) if len(row) > 5 else None
                        bal    = self._to_decimal(
                            self._strip_cr_dr(str(row[6])) if len(row) > 6 else ""
                        )

                        if debit and debit > 0:
                            txn_type, amount = "debit", debit
                        elif credit and credit > 0:
                            txn_type, amount = "credit", credit
                        else:
                            continue

                        if bal:
                            last_balance = bal

                        transactions.append(ParsedTransaction(
                            date=date_val, amount=amount, transaction_type=txn_type,
                            description=desc, raw_description=desc, balance_after=bal,
                        ))

        balance = ParsedBalance(balance=last_balance, snapshot_date=transactions[-1].date) \
            if transactions and last_balance else None

        return ParseResult(
            institution=self.institution, account_type=self.account_type,
            account_ref=account_ref, transactions=transactions,
            balance=balance, source_file=str(path),
        )
