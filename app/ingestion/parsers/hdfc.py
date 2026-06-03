"""
HDFC Bank statement parser.
Handles CSV (net-banking download), PDF (older Smart Statement), and
HTML (current Smart Statement — decrypted from AES-ECB payload).

CSV columns:
  Date | Narration | Value Dat | Debit Amount | Credit Amount | Chq/Ref Number | Closing Balance

HTML Smart Statement table columns:
  Date | Transaction details | Cheque/Ref No | Value date | Withdrawal | Deposit | Closing Balance

PDF table columns (7-col layout):
  Date | Narration | Value Dt | Ref No./Chq. No. | Withdrawal Amt.(Dr) | Deposit Amt.(Cr) | Closing Balance
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

DATE_FMTS = ["%d/%m/%y", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y", "%d %b %Y", "%d-%m-%y",
             "%d-%m-%Y", "%d/%m/%Y"]


class HdfcParser(BaseParser):
    institution  = "HDFC"
    account_type = "bank"

    def parse(self, path: Path) -> ParseResult:
        ext = path.suffix.lower()
        if ext == ".pdf":
            return self._parse_pdf(path)
        if ext == ".html" or ext == ".htm":
            return self._parse_html(path)
        return self._parse_csv(path)

    # ── CSV ──────────────────────────────────────────────────────────────────

    def _parse_csv(self, path: Path) -> ParseResult:
        raw = path.read_text(errors="ignore")
        lines = raw.splitlines()

        header_idx = None
        for i, line in enumerate(lines):
            low = line.lower()
            if "narration" in low or "debit amount" in low:
                header_idx = i
                break

        if header_idx is None:
            logger.warning("HDFC CSV: cannot find header in %s", path.name)
            return ParseResult(institution=self.institution, account_type=self.account_type,
                               source_file=str(path))

        account_ref = None
        for line in lines[:header_idx]:
            m = re.search(r"(\d{10,16})", line)
            if m:
                account_ref = m.group(1)[-4:]
                break

        try:
            df = pd.read_csv(path, skiprows=header_idx, on_bad_lines="skip")
        except Exception as exc:
            logger.error("HDFC CSV parse error %s: %s", path.name, exc)
            return ParseResult(institution=self.institution, account_type=self.account_type,
                               source_file=str(path))

        df.columns = [c.strip() for c in df.columns]

        col_map = {}
        for c in df.columns:
            cu = c.upper()
            if "DATE" in cu and "VALUE" not in cu:
                col_map.setdefault("DATE", c)
            elif "NARRATION" in cu or "DESCRIPTION" in cu or "PARTICULARS" in cu:
                col_map.setdefault("DESC", c)
            elif "VALUE" in cu and "DATE" in cu:
                col_map.setdefault("VDATE", c)
            elif "DEBIT" in cu or cu == "DR":
                col_map.setdefault("DR", c)
            elif "CREDIT" in cu or cu == "CR":
                col_map.setdefault("CR", c)
            elif "CHQ" in cu or "REF" in cu or "REFERENCE" in cu:
                col_map.setdefault("REF", c)
            elif "CLOSING" in cu or "BALANCE" in cu or cu == "BAL":
                col_map.setdefault("BAL", c)

        transactions: list[ParsedTransaction] = []
        last_balance = None

        for _, row in df.iterrows():
            date_val = self._parse_date(str(row.get(col_map.get("DATE", "Date"), "")).strip(), DATE_FMTS)
            if date_val is None:
                continue

            narr  = str(row.get(col_map.get("DESC", "Narration"), "")).strip()
            debit = self._to_decimal(row.get(col_map.get("DR", "Debit Amount")))
            credit = self._to_decimal(row.get(col_map.get("CR", "Credit Amount")))
            bal   = self._to_decimal(row.get(col_map.get("BAL", "Closing Balance")))
            ref   = str(row.get(col_map.get("REF", "Chq/Ref Number"), "")).strip()
            vdate = self._parse_date(str(row.get(col_map.get("VDATE", "Value Dat"), "")).strip(), DATE_FMTS)

            if debit and debit > 0:
                txn_type, amount = "debit", debit
            elif credit and credit > 0:
                txn_type, amount = "credit", credit
            else:
                continue

            if len(narr) > 300:
                continue

            if bal:
                last_balance = bal

            transactions.append(ParsedTransaction(
                date=date_val, amount=amount, transaction_type=txn_type,
                description=narr, raw_description=narr,
                reference_number=ref or None, value_date=vdate,
                balance_after=bal,
            ))

        balance = ParsedBalance(balance=last_balance, snapshot_date=transactions[-1].date) \
            if transactions and last_balance else None

        return ParseResult(
            institution=self.institution, account_type=self.account_type,
            account_ref=account_ref, transactions=transactions,
            balance=balance, source_file=str(path),
        )

    # ── HTML (Smart Statement) ────────────────────────────────────────────────

    def _parse_html(self, path: Path) -> ParseResult:
        """
        Parse the decrypted HDFC Smart Statement HTML file.

        Table structure:
          Date | Transaction details | Cheque/Ref No | Value date | Withdrawal | Deposit | Closing Balance
        """
        html = path.read_text(errors="ignore")

        # Extract account number
        account_ref = None
        m = re.search(r"(?:Account\s*(?:No\.?|Number)\s*[:\s]+|A/c\s+No\.?\s*[:\s]+)"
                      r"([xX*\d\s]{6,20})", html, re.I)
        if m:
            digits = re.sub(r"\D", "", m.group(1))
            if digits:
                account_ref = digits[-4:]
        if not account_ref:
            # Filename contains account: HDFC_1632_...
            fn_m = re.search(r"HDFC_(\d{4})_", path.name)
            if fn_m:
                account_ref = fn_m.group(1)

        # Extract all table rows via pandas
        try:
            from io import StringIO
            tables = pd.read_html(StringIO(html))
        except Exception:
            tables = []

        transactions: list[ParsedTransaction] = []
        last_balance = None

        for table in tables:
            if table.empty or len(table.columns) < 5:
                continue

            # Normalise column names
            cols = [str(c).upper().strip() for c in table.columns]
            idx = _detect_html_columns(cols)
            if idx["date"] is None:
                continue

            for _, row in table.iterrows():
                date_str = str(row.iloc[idx["date"]]).strip()
                date_val = self._parse_date(date_str, DATE_FMTS)
                if date_val is None:
                    continue

                desc = str(row.iloc[idx["desc"]]).strip() if idx["desc"] is not None else ""
                desc = re.sub(r"\s+", " ", desc)

                if len(desc) > 300:
                    continue

                debit  = self._to_decimal(row.iloc[idx["dr"]])  if idx["dr"]  is not None else None
                credit = self._to_decimal(row.iloc[idx["cr"]])  if idx["cr"]  is not None else None
                bal    = self._to_decimal(row.iloc[idx["bal"]]) if idx["bal"] is not None else None
                ref    = str(row.iloc[idx["ref"]]).strip()      if idx["ref"] is not None else None

                # "-" means empty in HDFC HTML statements
                if debit  is None or str(row.iloc[idx["dr"]]).strip()  in ("-", "", "nan"):
                    debit  = None
                if credit is None or str(row.iloc[idx["cr"]]).strip()  in ("-", "", "nan"):
                    credit = None

                if debit and debit > 0:
                    txn_type, amount = "debit", debit
                elif credit and credit > 0:
                    txn_type, amount = "credit", credit
                else:
                    continue

                if ref in ("-", "nan", "None", ""):
                    ref = None

                vdate = None
                if idx["vdate"] is not None:
                    vdate = self._parse_date(str(row.iloc[idx["vdate"]]).strip(), DATE_FMTS)

                if bal:
                    last_balance = bal

                transactions.append(ParsedTransaction(
                    date=date_val, amount=amount, transaction_type=txn_type,
                    description=desc, raw_description=desc,
                    reference_number=ref, value_date=vdate,
                    balance_after=bal,
                ))

        balance = ParsedBalance(balance=last_balance, snapshot_date=transactions[-1].date) \
            if transactions and last_balance else None

        logger.info("HDFC HTML parsed %d transactions from %s", len(transactions), path.name)
        return ParseResult(
            institution=self.institution, account_type=self.account_type,
            account_ref=account_ref, transactions=transactions,
            balance=balance, source_file=str(path),
        )

    # ── PDF ──────────────────────────────────────────────────────────────────

    def _parse_pdf(self, path: Path) -> ParseResult:
        """
        HDFC bank statement PDF — two known table layouts:

        Layout A (Smart Statement / older): one transaction per row, 7 cols.
        Layout B (NetBanking Acct_Statement): all transactions on a page are
          stacked into ONE row with \\n-separated values per cell.
          Amounts are computed via balance delta because the withdrawal/deposit
          columns have fewer lines than date/balance (empty cells omitted by pdfplumber).
        """
        transactions: list[ParsedTransaction] = []
        last_balance = None
        account_ref  = None
        prev_balance = None  # tracks closing balance across pages for Layout B delta math

        with self._open_pdf(path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""

                if account_ref is None:
                    m = (
                        re.search(r"Account\s*(?:No\.?|Number)\s*[:\s]+[xX*\d]+?(\d{4})\b", text, re.I)
                        or re.search(r"[xX*]{3,}\s*(\d{4})\b", text)
                        or re.search(r"\b(\d{14,16})\b", text)
                    )
                    if m:
                        account_ref = m.group(1)[-4:]

                # Try to extract opening balance from page text (needed for first transaction)
                if prev_balance is None:
                    ob_m = re.search(
                        r"(?:Opening\s+Bal(?:ance)?|Brought\s+Forward|B/?F)\s*[:\s]+([0-9,]+\.\d{2})",
                        text, re.I,
                    )
                    if ob_m:
                        prev_balance = self._to_decimal(ob_m.group(1))

                tables = page.extract_tables() or []
                for table in tables:
                    if not table:
                        continue

                    header_row = table[0]
                    header = [str(c or "").upper().replace("\n", " ").strip() for c in header_row]
                    idx = _detect_pdf_columns(header)

                    data_rows_raw = table[1:] if len(table) >= 2 else [table[0]]
                    if idx["dr"] is None and idx["bal"] is None and re.search(r"\d{2}/\d{2}/\d{2}", header[0]):
                        data_rows_raw = table

                    # Detect Layout B: any data row whose first cell contains \n
                    is_stacked = any("\n" in str(row[0] or "") for row in data_rows_raw if row)

                    if is_stacked:
                        # ── Layout B: balance-delta approach ─────────────────
                        ncols    = len(table[0]) if table else 0
                        bal_col  = idx["bal"]  if idx["bal"]  is not None else (ncols - 1)
                        desc_col = idx["desc"] if idx["desc"] is not None else 1
                        ref_col  = idx["ref"]
                        vdate_col = idx["vdate"]
                        # For standard 7-column HDFC layout (with or without a proper header),
                        # always use the known column positions.  When the table has no separate
                        # header row the data-row text can contain "BALANCE" / "CREDIT" etc,
                        # causing _detect_pdf_columns to map bal_col to the narration column
                        # (idx 1) instead of the actual last column.
                        # Date(0) | Narration(1) | Ref(2) | ValueDt(3) | Dr(4) | Cr(5) | Balance(6)
                        if ncols == 7:
                            bal_col   = 6
                            desc_col  = 1
                            if ref_col   is None: ref_col   = 2
                            if vdate_col is None: vdate_col = 3

                        for row in data_rows_raw:
                            if not row:
                                continue
                            first = str(row[0] or "")
                            if "\n" not in first:
                                # Non-stacked row in a stacked table (e.g. "Opening Balance" summary)
                                if prev_balance is None and bal_col < len(row):
                                    b = self._to_decimal(row[bal_col])
                                    if b is not None:
                                        prev_balance = b
                                continue

                            date_lines = [d.strip() for d in first.split("\n")]
                            bal_lines  = str(row[bal_col] or "").split("\n") if bal_col < len(row) else []

                            date_vals: list = []
                            bal_vals:  list = []
                            for d in date_lines:
                                dv = self._parse_date(d, DATE_FMTS)
                                if dv is not None:
                                    date_vals.append(dv)
                            for b in bal_lines:
                                bv = self._to_decimal(b.strip())
                                if bv is not None:
                                    bal_vals.append(bv)

                            if not date_vals or not bal_vals:
                                continue

                            n = min(len(date_vals), len(bal_vals))
                            date_vals = date_vals[:n]
                            bal_vals  = bal_vals[:n]

                            # Narration, ref, vdate — best-effort (narration may have extra continuation lines)
                            narr_lines  = str(row[desc_col]   or "").split("\n") if desc_col < len(row) else []
                            ref_lines   = str(row[ref_col]    or "").split("\n") if ref_col  is not None and ref_col  < len(row) else []
                            vdate_lines = str(row[vdate_col]  or "").split("\n") if vdate_col is not None and vdate_col < len(row) else []

                            # Pre-compute narration boundaries using ref numbers as anchors.
                            # Each narration block ends at (and includes) the line containing its ref.
                            # This handles multi-line narrations where simple index i fails.
                            narr_starts: list[int] = []
                            narr_search = 0
                            for i in range(n):
                                ref_raw = ref_lines[i].strip() if i < len(ref_lines) else ""
                                ref_sig = ref_raw.lstrip("0")  # significant digits (drop leading zeros)
                                found_end = None
                                if ref_sig:
                                    for j in range(narr_search, len(narr_lines)):
                                        line = narr_lines[j].strip()
                                        if line == ref_raw or (ref_sig and ref_sig in line):
                                            found_end = j  # inclusive end of this narration block
                                            break
                                narr_starts.append(narr_search)
                                if found_end is not None:
                                    narr_search = found_end + 1
                                else:
                                    narr_search += 1  # fallback: advance by 1 line

                            for i in range(n):
                                bal  = bal_vals[i]
                                prev = prev_balance if i == 0 else bal_vals[i - 1]

                                if prev is None:
                                    prev_balance = bal
                                    continue

                                delta = bal - prev
                                if delta == 0:
                                    continue

                                # Sanity check: reject balances that look like PDF extraction artifacts
                                # (spurious totals / page footers getting picked up as closing balances)
                                if abs(delta) > 100_000_000:
                                    logger.warning(
                                        "HDFC PDF %s: skipping implausible delta ₹%s on %s — likely bad balance extraction",
                                        path.name, abs(delta), date_vals[i],
                                    )
                                    continue

                                txn_type = "credit" if delta > 0 else "debit"
                                amount   = abs(delta)

                                # Reconstruct narration from all lines belonging to this transaction
                                start = narr_starts[i]
                                end   = narr_starts[i + 1] if i + 1 < n else len(narr_lines)
                                narr  = " ".join(l.strip() for l in narr_lines[start:end] if l.strip())

                                ref   = ref_lines[i].strip()   if i < len(ref_lines)   else None
                                vdate = (
                                    self._parse_date(vdate_lines[i].strip(), DATE_FMTS)
                                    if i < len(vdate_lines) else None
                                )

                                if ref in ("", "nan", "None"):
                                    ref = None
                                narr = narr[:300]

                                last_balance = bal
                                transactions.append(ParsedTransaction(
                                    date=date_vals[i], amount=amount, transaction_type=txn_type,
                                    description=narr, raw_description=narr,
                                    reference_number=ref or None, value_date=vdate,
                                    balance_after=bal,
                                ))

                            prev_balance = bal_vals[-1]

                    else:
                        # ── Layout A: one transaction per row ─────────────────
                        for row in data_rows_raw:
                            if not row:
                                continue

                            date_val = self._parse_date(str(row[0] or "").strip(), DATE_FMTS)
                            if date_val is None:
                                continue

                            if idx["desc"] is not None and idx["desc"] < len(row):
                                narr = str(row[idx["desc"]] or "").replace("\n", " ").strip()
                            elif len(row) > 1:
                                narr = str(row[1] or "").replace("\n", " ").strip()
                            else:
                                narr = ""

                            if len(narr) > 300:
                                narr = narr[:300]

                            debit  = self._to_decimal(row[idx["dr"]])  if idx["dr"]  is not None and idx["dr"]  < len(row) else None
                            credit = self._to_decimal(row[idx["cr"]])  if idx["cr"]  is not None and idx["cr"]  < len(row) else None
                            bal    = self._to_decimal(row[idx["bal"]]) if idx["bal"] is not None and idx["bal"] < len(row) else None
                            ref    = str(row[idx["ref"]] or "").strip() if idx["ref"] is not None and idx["ref"] < len(row) else None

                            if debit is None and credit is None:
                                ncol = len(row)
                                if ncol >= 7:
                                    debit  = self._to_decimal(row[4])
                                    credit = self._to_decimal(row[5])
                                    bal    = self._to_decimal(row[6])
                                elif ncol >= 6:
                                    debit  = self._to_decimal(row[3])
                                    credit = self._to_decimal(row[4])
                                    bal    = self._to_decimal(row[5])
                                elif ncol >= 5:
                                    debit  = self._to_decimal(row[2])
                                    credit = self._to_decimal(row[3])
                                    bal    = self._to_decimal(row[4])

                            if debit and debit > 0:
                                txn_type, amount = "debit", debit
                            elif credit and credit > 0:
                                txn_type, amount = "credit", credit
                            else:
                                continue

                            if bal:
                                last_balance = bal
                                prev_balance = bal

                            transactions.append(ParsedTransaction(
                                date=date_val, amount=amount, transaction_type=txn_type,
                                description=narr, raw_description=narr,
                                reference_number=ref or None,
                                balance_after=bal,
                            ))

        balance = ParsedBalance(balance=last_balance, snapshot_date=transactions[-1].date) \
            if transactions and last_balance else None

        return ParseResult(
            institution=self.institution, account_type=self.account_type,
            account_ref=account_ref, transactions=transactions,
            balance=balance, source_file=str(path),
        )


def _detect_html_columns(cols: list[str]) -> dict:
    """Map semantic roles to column indices from HDFC HTML Smart Statement header."""
    idx = {"date": None, "desc": None, "dr": None, "cr": None,
           "bal": None, "ref": None, "vdate": None}

    for i, h in enumerate(cols):
        if idx["date"]  is None and re.search(r"\bDATE\b", h) and "VALUE" not in h:
            idx["date"]  = i
        elif idx["desc"]  is None and re.search(r"NARR|DESCRIPTION|DETAIL|TRANSACTION|PARTICULAR", h):
            idx["desc"]  = i
        elif idx["dr"]    is None and re.search(r"WITHDRAW|DEBIT|\bDR\b", h):
            idx["dr"]    = i
        elif idx["cr"]    is None and re.search(r"DEPOSIT|CREDIT|\bCR\b", h):
            idx["cr"]    = i
        elif idx["bal"]   is None and re.search(r"CLOSING|BALANCE|\bBAL\b", h):
            idx["bal"]   = i
        elif idx["ref"]   is None and re.search(r"REF|CHQ|CHEQUE|REFERENCE", h):
            idx["ref"]   = i
        elif idx["vdate"] is None and re.search(r"VALUE\s*DATE|VALUE\s*DT", h):
            idx["vdate"] = i

    # Fallbacks if headers aren't descriptive
    if idx["date"] is None and cols:
        idx["date"] = 0
    if idx["desc"] is None and len(cols) > 1:
        idx["desc"] = 1

    return idx


def _detect_pdf_columns(header: list[str]) -> dict:
    """Map semantic roles to column indices from HDFC PDF table header."""
    idx = {"desc": None, "dr": None, "cr": None, "bal": None, "ref": None, "vdate": None}

    for i, h in enumerate(header):
        if idx["desc"]  is None and re.search(r"NARR|DESCRIPTION|PARTICULAR|DETAIL", h):
            idx["desc"]  = i
        elif idx["dr"]   is None and re.search(r"WITHDRAW|DEBIT|\bDR\b", h):
            idx["dr"]    = i
        elif idx["cr"]   is None and re.search(r"DEPOSIT|CREDIT|\bCR\b", h):
            idx["cr"]    = i
        elif idx["bal"]  is None and re.search(r"CLOSING|BALANCE|\bBAL\b", h):
            idx["bal"]   = i
        elif idx["ref"]  is None and re.search(r"REF|CHQ|CHEQUE|REFERENCE", h):
            idx["ref"]   = i
        elif idx["vdate"] is None and re.search(r"VALUE\s*DT|VALUE\s*DATE", h):
            idx["vdate"] = i

    return idx
