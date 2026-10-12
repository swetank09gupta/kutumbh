"""
SBI Bank statement parser — CSV and PDF.

CSV columns (net-banking download):
  Txn Date | Value Date | Description | Ref No./Cheque No. | Debit | Credit | Balance

PDF also extracts:
  - TDR/STDR fixed deposits → ParsedManualAsset (asset_type="fd")
  - PPF balance              → ParsedManualAsset (asset_type="ppf")
  - Closed FD account refs   → ParseResult.closed_account_refs
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from pathlib import Path

import pandas as pd
from app.ingestion.parsers.base import (
    BaseParser, ParsedBalance, ParsedManualAsset, ParsedTransaction, ParseResult,
)

logger = logging.getLogger(__name__)

DATE_FMTS = ["%d %b %Y", "%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y", "%d %b %y",
             "%d-%m-%y"]  # SBI e-statement uses DD-MM-YY (2-digit year)


class SbiParser(BaseParser):
    institution  = "SBI"
    account_type = "bank"

    def parse(self, path: Path) -> ParseResult:
        if path.suffix.lower() == ".pdf":
            return self._parse_pdf(path)
        return self._parse_csv(path)

    def _parse_csv(self, path: Path) -> ParseResult:
        raw = path.read_text(errors="ignore")
        lines = raw.splitlines()

        # Find header row
        header_idx = 0
        for i, line in enumerate(lines):
            if "Txn Date" in line or "Transaction Date" in line:
                header_idx = i
                break

        # Account number extraction
        account_ref = None
        for line in lines[:header_idx]:
            m = re.search(r"Account\s*(?:No|Number)[:\s]+(\d+)", line, re.IGNORECASE)
            if m:
                account_ref = m.group(1)[-4:]
                break

        try:
            df = pd.read_csv(path, skiprows=header_idx, on_bad_lines="skip")
        except Exception as exc:
            logger.error("SBI CSV parse error %s: %s", path.name, exc)
            return ParseResult(institution=self.institution, account_type=self.account_type)

        df.columns = [c.strip() for c in df.columns]

        transactions: list[ParsedTransaction] = []
        last_balance = None

        for _, row in df.iterrows():
            raw_date = str(row.get("Txn Date", row.get("Transaction Date", ""))).strip()
            date_val = self._parse_date(raw_date, DATE_FMTS)
            if date_val is None:
                continue

            desc   = str(row.get("Description", row.get("Particulars", ""))).strip()
            ref    = str(row.get("Ref No./Cheque No.", row.get("Ref No", ""))).strip()
            debit  = self._to_decimal(row.get("Debit"))
            credit = self._to_decimal(row.get("Credit"))
            bal    = self._to_decimal(row.get("Balance"))
            vdate  = self._parse_date(str(row.get("Value Date", "")), DATE_FMTS)

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
                description=desc, raw_description=desc,
                reference_number=ref or None, value_date=vdate, balance_after=bal,
            ))

        balance = ParsedBalance(balance=last_balance, snapshot_date=transactions[-1].date) \
            if transactions and last_balance else None

        return ParseResult(
            institution=self.institution, account_type=self.account_type,
            account_ref=account_ref, transactions=transactions,
            balance=balance, source_file=str(path),
        )

    # ── regex patterns for FD / PPF extraction ───────────────────────────────
    # 2025 format: TERM DEPOSIT XXXXX6925 11-10-24 200000.00 P 6.80 0.00 763.43 200000.00 11-10-25
    # 2026 format: TERM DEPOSIT XXXXXXX6925 11-10-24 140000.00 P SINGLE 6.25 0.00 491.29 139970.00 11-10-26
    # The optional mode word (SINGLE/JOINT/etc.) between P/S and ROI requires an updated pattern.
    _FD_LINE_RE = re.compile(
        r'TERM DEPOSIT\s+X+(\w{4})\s+(\d{2}-\d{2}-\d{2})\s+([\d,]+\.\d+)\s+[PS]'
        r'(?:\s+[A-Z]+(?:\s+[A-Z]+)*)?\s+([\d.]+)'
        r'\s+[\d.]+\s+([\d.]+)\s+([\d,]+\.\d+)\s+(\d{2}-\d{2}-\d{2})',          # lien, interest accrued, maturity amount, date
        re.I,
    )
    # ACCOUNT(S) CLOSED DURING THE MONTH rows
    _CLOSED_LINE_RE = re.compile(
        r'TERM DEPOSIT\s+X+(\w{4})\s+(\d{2}-\d{2}-\d{2})',
        re.I,
    )
    # "As on 31-07-25" or "As on 31-07-2025"
    _AS_ON_RE = re.compile(r'As on (\d{2}-\d{2}-\d{2,4})', re.I)
    # PPF balance in the account summary header
    _PPF_RE   = re.compile(r'PPF\s+INR\s+\d+\s+\d+\s+([\d,]+\.\d+)', re.I)
    # PPF account detail section
    _PPF_ACCT_RE = re.compile(
        r'PPF\s+ACCOUNT\s+.*?(\d{2}-\d{2}-\d{2,4})\s+([\d,]+\.\d+)',
        re.I | re.S,
    )

    def _extract_fd_ppf(self, full_text: str, owner_id: str) -> tuple[
        list[ParsedManualAsset], list[str], str | None, list[str]
    ]:
        """
        Parse the FD and PPF sections from the full PDF text.
        Returns: (manual_assets, closed_account_refs, statement_date_str, warnings)
        """
        from datetime import datetime

        manual_assets: list[ParsedManualAsset] = []
        closed_refs:   list[str] = []
        warnings:      list[str] = []

        # ── Statement date ────────────────────────────────────────────────────
        stmt_date: date | None = None
        m = self._AS_ON_RE.search(full_text)
        if m:
            raw = m.group(1)
            stmt_date = self._parse_date(raw, ["%d-%m-%y", "%d-%m-%Y"])

        # ── Active FDs ────────────────────────────────────────────────────────
        # Only look within the TDR/STDR section (stop before CLOSED section)
        tdr_idx    = full_text.upper().find("TDR AND STDR")
        closed_idx = full_text.upper().find("ACCOUNT(S) CLOSED DURING")
        if closed_idx == -1:
            closed_idx = len(full_text)

        if tdr_idx != -1:
            fd_section = full_text[tdr_idx:closed_idx]
            regex_hits = list(self._FD_LINE_RE.finditer(fd_section))

            if not regex_hits and "TERM DEPOSIT" in fd_section.upper():
                # Section exists but regex matched nothing — format likely changed.
                # Fall back to LLM extraction; it adapts to any layout automatically.
                msg = (
                    f"SBI FD: regex matched 0 records in TDR section ({len(fd_section)} chars). "
                    "Falling back to LLM extraction — check if PDF layout changed."
                )
                logger.warning(msg)
                warnings.append(msg)

                # Build a raw-line lookup keyed by last4 — used to extract dates/amounts
                # with reliable Python parsing instead of trusting the LLM for numerics.
                line_by_last4: dict[str, str] = {}
                for line in fd_section.splitlines():
                    if "TERM DEPOSIT" in line.upper() and any(c.isdigit() for c in line):
                        m4 = re.search(r'TERM DEPOSIT\s+X+(\w{4})', line, re.I)
                        if m4:
                            line_by_last4[m4.group(1)] = line

                for rec in self._llm_extract("bank_fd", fd_section):
                    try:
                        last4  = str(rec.get("last4") or "????")[-4:]
                        raw    = line_by_last4.get(last4, "")

                        # Dates: re-parse from raw line with Python (LLM mixes up DD/YY
                        # for end-of-month dates like 31-03-27 → wrong 2027-03-27).
                        raw_dates = re.findall(r'\d{2}-\d{2}-\d{2}', raw)
                        open_date = self._parse_date(raw_dates[0], ["%d-%m-%y"]) \
                            if raw_dates else \
                            self._parse_date(str(rec.get("open_date", "")), ["%Y-%m-%d"])
                        mat_date  = self._parse_date(raw_dates[-1], ["%d-%m-%y"]) \
                            if len(raw_dates) >= 2 else \
                            self._parse_date(str(rec.get("maturity_date", "")), ["%Y-%m-%d"])

                        # Amounts: first float in row = principal, last = maturity_amount.
                        # LLM often returns principal=null; raw line is ground truth.
                        raw_amounts = [
                            a.replace(",", "")
                            for a in re.findall(r'[\d,]+\.\d+', raw)
                        ]
                        principal  = Decimal(raw_amounts[0])  if raw_amounts else None
                        accrued_llm = Decimal(raw_amounts[-2]) if len(raw_amounts) >= 4 else None
                        mat_amount = Decimal(raw_amounts[-1]) if raw_amounts else \
                            (Decimal(str(rec["maturity_amount"])) if rec.get("maturity_amount") else None)

                        if not principal:
                            logger.warning("SBI LLM FD: no principal for last4=%s", last4)
                            continue

                        manual_assets.append(ParsedManualAsset(
                            asset_type="fd",
                            institution="SBI",
                            name=f"SBI FD ...{last4}",
                            current_value=principal,
                            account_ref=last4,
                            interest_rate=Decimal(str(rec["roi"])) if rec.get("roi") else None,
                            start_date=open_date,
                            maturity_date=mat_date,
                            maturity_amount=mat_amount,
                            accrued_interest=accrued_llm,
                            statement_date=stmt_date,
                        ))
                    except Exception as exc:
                        logger.warning("SBI LLM FD record conversion failed: %s — %s", rec, exc)
            else:
                for m in regex_hits:
                    last4, open_raw, principal_raw, roi_raw, accrued_raw, mat_amt_raw, mat_date_raw = m.groups()
                    open_date = self._parse_date(open_raw, ["%d-%m-%y", "%d-%m-%Y"])
                    mat_date  = self._parse_date(mat_date_raw, ["%d-%m-%y", "%d-%m-%Y"])
                    try:
                        principal  = Decimal(principal_raw.replace(",", ""))
                        accrued    = Decimal(accrued_raw)
                        roi        = Decimal(roi_raw)
                        mat_amount = Decimal(mat_amt_raw.replace(",", ""))
                    except Exception:
                        continue
                    manual_assets.append(ParsedManualAsset(
                        asset_type="fd",
                        institution="SBI",
                        name=f"SBI FD ...{last4}",
                        current_value=principal,
                        account_ref=last4,
                        interest_rate=roi,
                        start_date=open_date,
                        maturity_date=mat_date,
                        maturity_amount=mat_amount,
                        accrued_interest=accrued,
                        statement_date=stmt_date,
                    ))

        # ── Closed FD account refs ────────────────────────────────────────────
        if "ACCOUNT(S) CLOSED DURING" in full_text.upper():
            start = full_text.upper().find("ACCOUNT(S) CLOSED DURING")
            # Scan until the next section heading (TDR, TRANSACTION DETAILS, or end)
            end_markers = ["TRANSACTION DETAILS", "SAVING ACCOUNT\n", "AS PER PREVAILING"]
            end = len(full_text)
            for marker in end_markers:
                idx = full_text.upper().find(marker.upper(), start + 100)
                if idx != -1:
                    end = min(end, idx)
            closed_section = full_text[start:end]
            for m in self._CLOSED_LINE_RE.finditer(closed_section):
                closed_refs.append(m.group(1))

        # ── PPF balance ───────────────────────────────────────────────────────
        ppf_match = self._PPF_RE.search(full_text)
        if ppf_match:
            try:
                ppf_val = Decimal(ppf_match.group(1).replace(",", ""))
                if ppf_val > 0:
                    manual_assets.append(ParsedManualAsset(
                        asset_type="ppf",
                        institution="SBI",
                        name="SBI PPF",
                        current_value=ppf_val,
                        account_ref="ppf",
                        interest_rate=Decimal("7.1"),
                        statement_date=stmt_date,
                    ))
            except Exception:
                pass
        elif "PPF" in full_text.upper():
            # PPF keyword present but regex didn't match — try LLM
            msg = "SBI PPF: regex matched nothing despite PPF keyword in text. Falling back to LLM."
            logger.warning(msg)
            warnings.append(msg)
            for rec in self._llm_extract("bank_ppf", full_text):
                try:
                    ppf_val = Decimal(str(rec["balance"]))
                    if ppf_val > 0:
                        manual_assets.append(ParsedManualAsset(
                            asset_type="ppf",
                            institution="SBI",
                            name="SBI PPF",
                            current_value=ppf_val,
                            account_ref="ppf",
                            interest_rate=Decimal("7.1"),
                            statement_date=stmt_date,
                        ))
                except Exception as exc:
                    logger.warning("SBI LLM PPF record conversion failed: %s — %s", rec, exc)

        logger.info(
            "SBI PDF FD/PPF: %d active FDs, %d closed, PPF=%s (stmt date %s)",
            sum(1 for a in manual_assets if a.asset_type == "fd"),
            len(closed_refs),
            next((str(a.current_value) for a in manual_assets if a.asset_type == "ppf"), "none"),
            stmt_date,
        )
        return manual_assets, closed_refs, str(stmt_date) if stmt_date else None, warnings

    def _parse_pdf(self, path: Path) -> ParseResult:
        """
        SBI e-statement PDF (savings / current / FD multi-page):

        Table layouts observed in the wild (pdfplumber column count):
          8-col: [Date, Desc, None, None, Ref, Credit, Debit, Balance]  ← most common
          7-col: [Date, Desc, None, Ref,  Credit, Debit, Balance]
          6-col: [Date, Desc, Ref,  Credit, Debit, Balance]

        Credit appears BEFORE Debit in all layouts.
        Date format: DD-MM-YY (2-digit year).

        Also extracts TDR/STDR FDs and PPF balance → ParsedManualAsset list.
        """
        transactions: list[ParsedTransaction] = []
        last_balance = None
        account_ref  = None
        full_text    = ""

        with self._open_pdf(path) as pdf:
            pages_text = []
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                pages_text.append(page_text)

                # Extract savings account ref from page text
                if account_ref is None:
                    m = re.search(r"Account\s+Number\s+[\w*X]+(\d{4})", page_text) or \
                        re.search(r"X{3,}(\d{4})\s", page_text)
                    if m:
                        account_ref = m.group(1)

                for table in (page.extract_tables() or []):
                    cr_col = dr_col = bal_col = date_col = desc_col = None

                    for row in table:
                        if not row:
                            continue
                        ncol = len(row)

                        if cr_col is None:
                            row_str = [str(c or "").lower() for c in row]
                            if "credit" in row_str and "debit" in row_str:
                                cr_col   = next((i for i, c in enumerate(row_str) if c == "credit"), None)
                                dr_col   = next((i for i, c in enumerate(row_str) if c == "debit"),  None)
                                bal_col  = next((i for i, c in enumerate(row_str) if "balance" in c), None)
                                date_col = 0
                                desc_col = 1
                                logger.debug("SBI PDF layout detected: cr=%s dr=%s bal=%s", cr_col, dr_col, bal_col)
                                continue

                        if cr_col is None:
                            if ncol >= 8:
                                date_col, desc_col, cr_col, dr_col, bal_col = 0, 1, 5, 6, 7
                            elif ncol == 7:
                                date_col, desc_col, cr_col, dr_col, bal_col = 0, 1, 4, 5, 6
                            elif ncol == 6:
                                date_col, desc_col, cr_col, dr_col, bal_col = 0, 1, 3, 4, 5
                            elif ncol == 5:
                                date_col, desc_col, cr_col, dr_col, bal_col = 0, 2, None, 3, 4
                            else:
                                continue

                        if date_col is None or ncol <= max(filter(None, [date_col, desc_col, dr_col, bal_col])):
                            continue

                        date_val = self._parse_date(str(row[date_col] or ""), DATE_FMTS)
                        if date_val is None:
                            continue

                        desc   = str(row[desc_col] or "").strip().replace("\n", " ")
                        credit = self._to_decimal(row[cr_col]) if cr_col is not None and cr_col < ncol else None
                        debit  = self._to_decimal(row[dr_col]) if dr_col < ncol else None
                        bal    = self._to_decimal(row[bal_col]) if bal_col is not None and bal_col < ncol else None

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

            full_text = "\n".join(pages_text)

        balance = ParsedBalance(balance=last_balance, snapshot_date=transactions[-1].date) \
            if transactions and last_balance else None

        # ── FD + PPF extraction ───────────────────────────────────────────────
        manual_assets, closed_refs, _, warnings = self._extract_fd_ppf(full_text, "")

        return ParseResult(
            institution=self.institution, account_type=self.account_type,
            account_ref=account_ref, transactions=transactions,
            manual_assets=manual_assets, closed_account_refs=closed_refs,
            balance=balance, source_file=str(path),
            parse_warnings=warnings,
        )
