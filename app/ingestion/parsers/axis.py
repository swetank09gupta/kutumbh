"""
Axis Bank statement parser — CSV and PDF.

CSV columns: Tran Date | CHQNO | PARTICULARS | DR | CR | BAL

PDF handling: Axis Bank encrypts monthly statements with a password
(typically first 4 chars of name + DOB, passed via pdf_password arg). The transaction table is read by column
position (axis_layout) and must reconcile to the statement's own opening and closing balance; running term deposits
are read from its "Summary of Deposits".
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
from app.ingestion.parsers.base import (
    BaseParser, ParsedBalance, ParsedManualAsset, ParsedTransaction, ParseResult,
)

logger = logging.getLogger(__name__)

DATE_FMTS = ["%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%d-%m-%y",
             "%d/%m/%y", "%d %B %Y"]



class AxisParser(BaseParser):
    _PARSER_VERSION = "+axis2"      # a change here re-reads only Axis files (same key as before: nothing is invalidated)
    institution  = "Axis"
    account_type = "bank"

    def parse(self, path: Path) -> ParseResult:
        if path.suffix.lower() == ".pdf":
            return self._parse_pdf(path)
        return self._parse_csv(path)

    # ── CSV ──────────────────────────────────────────────────────────────────

    def _parse_csv(self, path: Path) -> ParseResult:
        raw = path.read_text(errors="ignore")
        lines = raw.splitlines()

        header_idx = 0
        for i, line in enumerate(lines):
            low = line.lower()
            if "tran date" in low or "particulars" in low:
                header_idx = i
                break

        account_ref = None
        for line in lines[:header_idx]:
            m = re.search(r"(\d{14,18})", line)
            if m:
                account_ref = m.group(1)[-4:]
                break

        try:
            df = pd.read_csv(path, skiprows=header_idx, on_bad_lines="skip")
        except Exception as exc:
            logger.error("Axis CSV parse error %s: %s", path.name, exc)
            return ParseResult(institution=self.institution, account_type=self.account_type)

        df.columns = [c.strip().upper() for c in df.columns]

        col_map = {}
        for c in df.columns:
            if "DATE" in c:
                col_map["DATE"] = c
            elif "PARTICULARS" in c or "NARRATION" in c or "DESC" in c or "DETAILS" in c:
                col_map["DESC"] = c
            elif c in ("DR", "DEBIT"):
                col_map["DR"] = c
            elif c in ("CR", "CREDIT"):
                col_map["CR"] = c
            elif c in ("BAL", "BALANCE"):
                col_map["BAL"] = c

        transactions: list[ParsedTransaction] = []
        last_balance = None

        for _, row in df.iterrows():
            date_val = self._parse_date(str(row.get(col_map.get("DATE", ""), "")).strip(), DATE_FMTS)
            if date_val is None:
                continue

            desc   = str(row.get(col_map.get("DESC", ""), "")).strip()
            debit  = self._to_decimal(row.get(col_map.get("DR", "DR")))
            credit = self._to_decimal(row.get(col_map.get("CR", "CR")))
            bal    = self._to_decimal(row.get(col_map.get("BAL", "BAL")))

            if debit and debit > 0:
                txn_type, amount = "debit", debit
            elif credit and credit > 0:
                txn_type, amount = "credit", credit
            else:
                continue

            # Skip rows whose description is statement boilerplate (too long to be real)
            if len(desc) > 300:
                logger.debug("Skipping row with long description (%d chars) — likely boilerplate", len(desc))
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

    # ── PDF ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _account_ref(text: str) -> str | None:
        """The statement's own account number. A long masked number (XXXXXXXXXXX3856) is reduced to its last four digits; a
        10-character one is kept as printed (50410XXXXX, XXXXXX3237: the ex-Citi account in two layouts) because its width is
        what lets maintenance see they are one number. Without any, every statement lands on "unknown"."""
        t = re.sub(r"\(cid:\d+\)", " ", text)
        m = re.search(r"(?i)(?:a/c\s*no\.?|account\s*(?:no\.?|number))\s*[:.]?\s*([0-9X*]{6,})", t)
        if not m:
            return None
        num = m.group(1).upper()
        return num[-4:] if num[-4:].isdigit() and len(num) > 10 else num

    @staticmethod
    def _empty_statement(text: str):
        """A statement with no transactions whose opening and closing balances agree (a dormant account): reconciled
        by definition, nothing to store but the balance. (closing, period end) or None."""
        o = re.search(r"Opening Balance\s+([\d,]+\.\d\d)", text)
        c = re.search(r"Closing Balance\s+([\d,]+\.\d\d)", text)
        sep = r"(?:\s|\(cid:\d+\))+"                       # pdfplumber prints a tab as "(cid:9)"
        period = re.search(rf"for{sep}the{sep}period{sep}from{sep}\d{{2}}-\d{{2}}-\d{{4}}{sep}to{sep}(\d{{2}}-\d{{2}}-\d{{4}})", text)
        if not (o and c and period and o.group(1) == c.group(1)) or re.search(r"^\d{2}-\d{2}-\d{4}\s", text, re.M):
            return None
        return Decimal(c.group(1).replace(",", "")), datetime.strptime(period.group(1), "%d-%m-%Y").date()

    def _parse_pdf(self, path: Path) -> ParseResult:
        """The statement's transaction table by column position (axis_layout), accepted only if it reconciles to the
        opening and closing balance the statement prints; otherwise nothing is stored (a wrong amount is worse than none)."""
        from app.ingestion.parsers import axis_layout as layout

        with self._open_pdf(path) as pdf:
            pages = [p.extract_words() for p in pdf.pages]
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        blocks = [b for b in layout.read_blocks(pages) if b.kind == "savings"]
        ref = self._account_ref(text)
        warnings: list[str] = []
        if not blocks and (empty := self._empty_statement(text)):
            return ParseResult(institution=self.institution, account_type=self.account_type, source_file=str(path),
                               account_ref=ref, balance=ParsedBalance(balance=empty[0], snapshot_date=empty[1]),
                               statement_date=empty[1])
        if not blocks or not all(b.reconciles for b in blocks):
            warnings.append(f"Axis {path.name}: the transaction table does not reconcile to the printed opening/closing "
                            "balance; no transactions stored from this file")
            logger.error(warnings[-1])
            return ParseResult(institution=self.institution, account_type=self.account_type, source_file=str(path),
                               account_ref=ref, parse_warnings=warnings)
        txns = [ParsedTransaction(date=r.day, amount=r.amount, transaction_type=r.kind, description=r.narration,
                                  raw_description=r.narration, balance_after=r.balance)
                for b in blocks for r in b.rows if r.amount > 0]
        period = re.search(r"between\s+(\d{2}-\d{2}-\d{4})\s+to\s+(\d{2}-\d{2}-\d{4})", text)
        stmt_end = self._parse_date(period.group(2), DATE_FMTS) if period else (txns[-1].date if txns else None)
        deposits = []
        for d in layout.read_term_deposits(text):
            if stmt_end and d.maturity >= stmt_end >= d.start:               # still running at the statement date
                deposits.append(ParsedManualAsset(
                    asset_type="fd", institution="Axis", name=f"Axis FD ...{d.number_tail}", current_value=d.principal,
                    account_ref=d.number_tail, interest_rate=d.rate, start_date=d.start, maturity_date=d.maturity,
                    maturity_amount=d.maturity_value, statement_date=stmt_end, principal_as_of=d.start))
        balance = ParsedBalance(balance=blocks[-1].closing, snapshot_date=stmt_end) if stmt_end else None
        return ParseResult(institution=self.institution, account_type=self.account_type, account_ref=ref,
                           transactions=txns, manual_assets=deposits, balance=balance, source_file=str(path), parse_warnings=warnings,
                           statement_date=stmt_end)
