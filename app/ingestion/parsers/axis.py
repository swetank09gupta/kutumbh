"""
Axis Bank statement parser — CSV and PDF.

CSV columns: Tran Date | CHQNO | PARTICULARS | DR | CR | BAL

PDF handling: Axis Bank encrypts monthly statements with a password
(typically first 4 chars of name + DOB, passed via pdf_password arg).
The parser tries table extraction first, with automatic column detection,
then falls back to text-line extraction if tables yield mostly empty narrations.
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

DATE_FMTS = ["%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%d-%m-%y",
             "%d/%m/%y", "%d %B %Y"]

# Axis Bank statement text-mode date pattern  (e.g. "05-10-2025")
_DATE_RE = re.compile(r"^(\d{2}[-/]\d{2}[-/]\d{2,4})\s+(.+?)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s*$")
# Simpler pattern when balance column is separate
_DATE_START = re.compile(r"^\d{2}[-/]\d{2}[-/]\d{2,4}")


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
        transactions: list[ParsedTransaction] = []
        last_balance = None
        account_ref  = None

        with self._open_pdf(path) as pdf:
            for page in pdf.pages:
                # Try to get account ref from first page text
                if account_ref is None:
                    txt = page.extract_text() or ""
                    m = re.search(r"Account\s+(?:Number|No\.?)\s*[:\-]?\s*(\d{10,18})", txt, re.I)
                    if m:
                        account_ref = m.group(1)[-4:]

                # ── attempt 1: table extraction ───────────────────────────
                table_txns, table_bal = self._extract_from_tables(page)
                if table_txns:
                    transactions.extend(table_txns)
                    if table_bal:
                        last_balance = table_bal
                    continue

                # ── attempt 2: text-line extraction ───────────────────────
                text_txns, text_bal = self._extract_from_text(page)
                transactions.extend(text_txns)
                if text_bal:
                    last_balance = text_bal

        # Deduplicate by (date, amount, type) keeping last occurrence
        seen: set = set()
        deduped = []
        for t in transactions:
            key = (t.date, t.amount, t.transaction_type)
            if key not in seen:
                seen.add(key)
                deduped.append(t)

        balance = ParsedBalance(balance=last_balance, snapshot_date=deduped[-1].date) \
            if deduped and last_balance else None

        return ParseResult(
            institution=self.institution, account_type=self.account_type,
            account_ref=account_ref, transactions=deduped,
            balance=balance, source_file=str(path),
        )

    def _extract_from_tables(self, page) -> tuple[list[ParsedTransaction], object]:
        """Extract transactions via pdfplumber table detection."""
        transactions = []
        last_balance = None

        for table in (page.extract_tables() or []):
            if not table or len(table) < 2:
                continue

            # Auto-detect column indices from header row
            header = [str(c or "").upper().replace("\n", " ").strip() for c in table[0]]
            idx = _detect_columns(header)
            if idx["date"] is None or idx["amount"] is None:
                continue

            for row in table[1:]:
                if not row or len(row) <= max(v for v in idx.values() if v is not None):
                    continue

                date_val = self._parse_date(str(row[idx["date"]] or "").strip(), DATE_FMTS)
                if date_val is None:
                    continue

                # Description: prefer dedicated column, fall back to adjacent non-null cells
                desc = ""
                if idx["desc"] is not None:
                    desc = str(row[idx["desc"]] or "").replace("\n", " ").strip()
                if not desc:
                    # Try columns around date for any non-numeric, non-empty content
                    for ci, cell in enumerate(row):
                        if ci == idx["date"] or ci == idx["amount"]:
                            continue
                        cell_s = str(cell or "").replace("\n", " ").strip()
                        if cell_s and not re.match(r"^[\d,. ]+$", cell_s):
                            desc = cell_s
                            break

                debit  = self._to_decimal(row[idx["dr"]]) if idx["dr"] is not None else None
                credit = self._to_decimal(row[idx["cr"]]) if idx["cr"] is not None else None
                bal    = self._to_decimal(row[idx["bal"]]) if idx["bal"] is not None else None

                # If single amount column, figure type from sign or surrounding context
                if debit is None and credit is None and idx["amount"] is not None:
                    amt = self._to_decimal(row[idx["amount"]])
                    if amt is None:
                        continue
                    # Try type column
                    txn_type = "debit"
                    if idx["type"] is not None:
                        t_str = str(row[idx["type"]] or "").upper()
                        if "CR" in t_str or "CREDIT" in t_str:
                            txn_type = "credit"
                    debit  = amt if txn_type == "debit"  else None
                    credit = amt if txn_type == "credit" else None

                if debit and debit > 0:
                    txn_type, amount = "debit", debit
                elif credit and credit > 0:
                    txn_type, amount = "credit", credit
                else:
                    continue

                if len(desc) > 300:
                    logger.debug("Skipping table row with long description (%d chars)", len(desc))
                    continue

                if bal:
                    last_balance = bal

                transactions.append(ParsedTransaction(
                    date=date_val, amount=amount, transaction_type=txn_type,
                    description=desc, raw_description=desc, balance_after=bal,
                ))

        return transactions, last_balance

    def _extract_from_text(self, page) -> tuple[list[ParsedTransaction], object]:
        """Fallback: parse page text line by line looking for date-anchored rows."""
        transactions = []
        last_balance = None
        text = page.extract_text() or ""
        lines = text.splitlines()

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not _DATE_START.match(line):
                i += 1
                continue

            # Accumulate continuation lines (no date prefix) into the description
            combined = line
            j = i + 1
            while j < len(lines) and not _DATE_START.match(lines[j].strip()):
                combined += " " + lines[j].strip()
                j += 1

            txn = self._parse_text_line(combined)
            if txn:
                transactions.append(txn)
                if txn.balance_after:
                    last_balance = txn.balance_after
            i = j

        return transactions, last_balance

    def _parse_text_line(self, line: str):
        """Parse a single combined text line into a ParsedTransaction."""
        # Extract trailing numbers (debit/credit/balance or just two numbers)
        nums = re.findall(r"[\d,]+\.\d{2}", line)
        if len(nums) < 2:
            return None

        # First token group = date
        date_m = re.match(r"(\d{2}[-/]\d{2}[-/]\d{2,4})\s*(.*)", line)
        if not date_m:
            return None

        date_val = self._parse_date(date_m.group(1), DATE_FMTS)
        if not date_val:
            return None

        rest = date_m.group(2)
        # Strip trailing numbers to get description
        desc = rest
        for n in nums:
            desc = desc.replace(n, "").strip()
        desc = re.sub(r"\s{2,}", " ", desc).strip(" ,:-")

        bal    = self._to_decimal(nums[-1])
        amount = self._to_decimal(nums[-2])
        if not amount:
            return None

        # Determine type: if three numbers present, position matters:
        # date | desc | DR | CR | BAL  — one of DR/CR is 0 or absent
        # Heuristic: if there are 3 numbers, check if second-to-last looks like debit/credit
        txn_type = "debit"
        if len(nums) >= 3:
            dr = self._to_decimal(nums[-3])
            cr = self._to_decimal(nums[-2])
            if dr and not cr:
                txn_type, amount = "debit", dr
            elif cr and not dr:
                txn_type, amount = "credit", cr
        # If only 2 nums, can't tell — try description hint
        if "CR" in desc.upper().split() or "CREDIT" in desc.upper():
            txn_type = "credit"

        return ParsedTransaction(
            date=date_val, amount=amount, transaction_type=txn_type,
            description=desc, raw_description=desc, balance_after=bal,
        )


def _detect_columns(header: list[str]) -> dict:
    """Map semantic roles to column indices from a header row."""
    idx = {"date": None, "desc": None, "dr": None, "cr": None,
           "bal": None, "amount": None, "type": None}

    for i, h in enumerate(header):
        if idx["date"] is None and re.search(r"\bDATE\b", h):
            idx["date"] = i
        elif idx["desc"] is None and re.search(r"NARR|PARTICULAR|DETAIL|DESCRIPTION|REMARK", h):
            idx["desc"] = i
        elif idx["dr"] is None and re.search(r"\bDEBIT\b|\bDR\b", h):
            idx["dr"] = i
        elif idx["cr"] is None and re.search(r"\bCREDIT\b|\bCR\b", h):
            idx["cr"] = i
        elif idx["bal"] is None and re.search(r"\bBALANCE\b|\bBAL\b", h):
            idx["bal"] = i
        elif idx["amount"] is None and re.search(r"\bAMOUNT\b|\bAMT\b", h):
            idx["amount"] = i
        elif idx["type"] is None and re.search(r"\bTYPE\b|\bDR/CR\b", h):
            idx["type"] = i

    # Fallback: if no date found, col 0 is usually date in Axis statements
    if idx["date"] is None and header:
        idx["date"] = 0
    # If no explicit desc col, use col 1 (narration is usually after date)
    if idx["desc"] is None and len(header) > 1:
        idx["desc"] = 1

    return idx
