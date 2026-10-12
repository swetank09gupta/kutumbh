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
from pathlib import Path

import pandas as pd
from app.ingestion.parsers.base import (
    BaseParser, ParsedBalance, ParsedManualAsset, ParsedTransaction, ParseResult,
)

logger = logging.getLogger(__name__)

DATE_FMTS = ["%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%d-%m-%y",
             "%d/%m/%y", "%d %B %Y"]



class AxisParser(BaseParser):
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

    def _parse_pdf(self, path: Path) -> ParseResult:
        """The statement's transaction table by column position (axis_layout), accepted only if it reconciles to the
        opening and closing balance the statement prints; otherwise nothing is stored (a wrong amount is worse than none)."""
        from app.ingestion.parsers import axis_layout as layout

        with self._open_pdf(path) as pdf:
            pages = [p.extract_words() for p in pdf.pages]
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        blocks = [b for b in layout.read_blocks(pages) if b.kind == "savings"]
        warnings: list[str] = []
        if not blocks or not all(b.reconciles for b in blocks):
            warnings.append(f"Axis {path.name}: the transaction table does not reconcile to the printed opening/closing "
                            "balance; no transactions stored from this file")
            logger.error(warnings[-1])
            return ParseResult(institution=self.institution, account_type=self.account_type, source_file=str(path),
                               parse_warnings=warnings)
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
                    maturity_amount=d.maturity_value, statement_date=stmt_end))
        balance = ParsedBalance(balance=blocks[-1].closing, snapshot_date=stmt_end) if stmt_end else None
        return ParseResult(institution=self.institution, account_type=self.account_type, transactions=txns,
                           manual_assets=deposits, balance=balance, source_file=str(path), parse_warnings=warnings,
                           statement_date=stmt_end)
