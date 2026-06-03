"""
CDSL Consolidated Account Statement (CAS) parser.

Sent monthly from eCAS@cdslstatement.com.  Password = PAN uppercase.

PDF structure (per page):
  Summary/cover pages  — skipped
  Transaction pages    — table: ISIN | Security | Particulars | Date | Op.Bal | Credit | Debit | Cl.Bal | Duty
  Holdings pages       — table: ISIN | Security | CurrBal | FrozenBal | PledgeBal | PledgeSetupBal | FreeBal | Price | Value

Uses pdfplumber table extraction (not raw text) for clean, reliable parsing.
"""
from __future__ import annotations

import calendar
import logging
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

from app.ingestion.parsers.base import (
    BaseParser, ParsedHolding, ParsedTransaction, ParseResult,
)

logger = logging.getLogger(__name__)

_DATE_FMTS = ["%d-%m-%Y", "%d-%b-%Y", "%d/%m/%Y"]

_MONTHS = {m.upper(): i for i, m in enumerate(calendar.month_abbr) if m}

def _statement_date_from_filename(path: Path) -> date | None:
    """
    CDSL CAS files are named e.g. MAY2025_AA20662210_TXN.pdf.
    Extract the last day of the named month as the statement date.
    """
    m = re.search(r'([A-Z]{3})(\d{4})_', path.name.upper())
    if not m:
        return None
    mon_abbr, year_str = m.group(1), m.group(2)
    mon_num = _MONTHS.get(mon_abbr)
    if not mon_num:
        return None
    year = int(year_str)
    last_day = calendar.monthrange(year, mon_num)[1]
    return date(year, mon_num, last_day)


def _is_valid_isin(s: str) -> bool:
    return bool(s and re.match(r'^[A-Z]{2}[A-Z0-9]{9}[0-9]$', s.strip()))


def _to_dec(val: str | None) -> Optional[Decimal]:
    if not val or val.strip() in ("--", "-", ""):
        return None
    try:
        return Decimal(val.strip().replace(",", ""))
    except Exception:
        return None


def _header_keys(row: list) -> list[str]:
    """Normalise a header row into lowercase single-line keys."""
    return [str(c or "").replace("\n", " ").strip().lower() for c in row]


def _is_holdings_header(keys: list[str]) -> bool:
    return (
        any("free bal" in k for k in keys)
        and any("current" in k or "curr" in k for k in keys)
        and any("value" in k for k in keys)
    )


def _is_txn_header(keys: list[str]) -> bool:
    return (
        any("credit" in k for k in keys)
        and any("debit" in k for k in keys)
        and any("date" in k for k in keys)
    )


class CdslCasParser(BaseParser):
    institution  = "CDSL"
    account_type = "demat"

    def parse(self, path: Path) -> ParseResult:
        holdings:     dict[str, ParsedHolding]    = {}  # keyed by ISIN
        transactions: list[ParsedTransaction]      = []
        txn_seen:     set[tuple]                   = set()
        account_ref:  Optional[str]                = None
        stmt_date     = _statement_date_from_filename(path)

        with self._open_pdf(path) as pdf:
            # Pull account ref from first-page text
            first_text = pdf.pages[0].extract_text() or ""
            account_ref = self._extract_account_ref(
                "\n".join(p.extract_text() or "" for p in pdf.pages[:4])
            )

            for page in pdf.pages:
                for table in (page.extract_tables() or []):
                    if not table or len(table) < 2:
                        continue
                    keys = _header_keys(table[0])

                    if _is_holdings_header(keys):
                        self._parse_holdings_table(table, holdings)
                    elif _is_txn_header(keys):
                        self._parse_txn_table(table, transactions, txn_seen)

        holding_list = list(holdings.values())
        logger.info(
            "CDSL CAS %s: %d holdings, %d transactions (acct=%s)",
            path.name, len(holding_list), len(transactions), account_ref,
        )
        return ParseResult(
            institution=self.institution,
            account_type=self.account_type,
            account_ref=account_ref,
            holdings=holding_list,
            transactions=transactions,
            complete_holdings_snapshot=bool(holding_list),
            source_file=str(path),
            statement_date=stmt_date,
        )

    # ── Account reference ─────────────────────────────────────────────────────

    def _extract_account_ref(self, text: str) -> Optional[str]:
        m = re.search(r'BO\s*ID\s*[:\s]+(\d{14,18})', text, re.I)
        if m:
            return m.group(1)
        m = re.search(r'DP\s*Id[:\s]*(\d+)\s+Client\s*Id\s*[:\s]*(\d+)', text, re.I)
        if m:
            return m.group(1) + m.group(2)
        return None

    # ── Holdings ─────────────────────────────────────────────────────────────

    def _parse_holdings_table(
        self, table: list[list], holdings: dict[str, ParsedHolding]
    ) -> None:
        """
        Table columns (9):
          ISIN | Security | CurrBal | FrozenBal | PledgeBal | PledgeSetupBal
          | FreeBal | Price/FaceVal | Value(`)
        """
        keys = _header_keys(table[0])

        isin_col  = next((i for i, k in enumerate(keys) if k == "isin"), 0)
        name_col  = next((i for i, k in enumerate(keys) if "security" in k), 1)
        curr_col  = next((i for i, k in enumerate(keys) if "current" in k or "curr" in k), 2)
        free_col  = next((i for i, k in enumerate(keys) if "free bal" in k), 6)
        price_col = next((i for i, k in enumerate(keys) if "price" in k or "face" in k), 7)
        # "value (`)" must not match "market price / face value" — exclude price/face/market
        val_col   = next(
            (i for i, k in enumerate(keys)
             if "value" in k and "price" not in k and "face" not in k and "market" not in k),
            8,
        )

        for row in table[1:]:
            if not row or len(row) <= val_col:
                continue

            isin = str(row[isin_col] or "").strip()
            if not _is_valid_isin(isin):
                continue

            name     = str(row[name_col] or "").replace("\n", " ").strip()
            name     = re.sub(r'[#\s]+', ' ', name).strip()
            curr_bal = _to_dec(str(row[curr_col] or ""))
            free_raw = str(row[free_col] or "").strip()
            free_bal = _to_dec(free_raw) if free_raw not in ("--", "") else curr_bal
            price    = _to_dec(str(row[price_col] or ""))
            value    = _to_dec(str(row[val_col] or ""))

            if not curr_bal or curr_bal <= 0:
                continue

            htype = (
                "mutual_fund" if isin.startswith("INF")
                else "bonds"  if isin.startswith("IN0")
                else "equity"
            )

            if isin in holdings:
                # Same ISIN on a second page — aggregate (pagination duplication)
                existing = holdings[isin]
                existing.units         = (existing.units or Decimal(0)) + curr_bal
                existing.current_value = (existing.current_value or Decimal(0)) + (value or Decimal(0))
                logger.debug("CDSL CAS: aggregated duplicate ISIN %s across pages", isin)
            else:
                holdings[isin] = ParsedHolding(
                    instrument_name=name or isin,
                    symbol=name or isin,
                    isin=isin,
                    units=curr_bal,
                    avg_cost_nav=None,    # CDSL CAS does not carry avg cost
                    current_nav=price,
                    current_value=value or Decimal(0),
                    invested_value=None,
                    holding_type=htype,
                )

    # ── Transactions ──────────────────────────────────────────────────────────

    def _parse_txn_table(
        self, table: list[list], transactions: list[ParsedTransaction], seen: set[tuple]
    ) -> None:
        """
        Table columns (9):
          ISIN | Security | Particulars | Date | Op.Bal | Credit | Debit | Cl.Bal | StampDuty

        Continuation rows (same ISIN, later dates) have empty ISIN/Security cells.
        """
        keys = _header_keys(table[0])

        isin_col   = next((i for i, k in enumerate(keys) if k == "isin"), 0)
        name_col   = next((i for i, k in enumerate(keys) if "security" in k), 1)
        date_col   = next((i for i, k in enumerate(keys) if "date" in k), 3)
        credit_col = next((i for i, k in enumerate(keys) if "credit" in k), 5)
        debit_col  = next((i for i, k in enumerate(keys) if "debit" in k), 6)

        current_isin = ""
        current_name = ""

        for row in table[1:]:
            if not row or len(row) <= debit_col:
                continue

            isin_raw = str(row[isin_col] or "").strip()
            if _is_valid_isin(isin_raw):
                current_isin = isin_raw
                current_name = str(row[name_col] or "").replace("\n", " ").strip()

            if not current_isin:
                continue

            date_str = str(row[date_col] or "").strip()
            date_val = self._parse_date(date_str, _DATE_FMTS)
            if date_val is None:
                continue

            credit = _to_dec(str(row[credit_col] or ""))
            debit  = _to_dec(str(row[debit_col] or ""))

            if credit and credit > 0:
                qty, txn_type = credit, "credit"
            elif debit and debit > 0:
                qty, txn_type = debit, "debit"
            else:
                continue

            key = (current_isin, str(date_val), str(qty), txn_type)
            if key in seen:
                continue
            seen.add(key)

            desc = f"DEMAT {txn_type.upper()} {qty} {current_name or current_isin}"
            transactions.append(ParsedTransaction(
                date=date_val,
                amount=qty,       # share units; ₹ value not available in CDSL CAS
                transaction_type=txn_type,
                description=desc,
                raw_description=current_name or current_isin,
                counterparty=current_name or None,
                isin=current_isin,
            ))
