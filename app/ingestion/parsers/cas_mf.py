"""
CAMS / KFintech Consolidated Account Statement (CAS) parser.
Uses the `casparser` library which handles the PDF password-protection
and returns structured folio + transaction data.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

from app.ingestion.parsers.base import (
    BaseParser, ParsedHolding, ParsedTransaction, ParseResult,
)

logger = logging.getLogger(__name__)


class CasMfParser(BaseParser):
    institution  = "CAMS/KFintech"
    account_type = "mutual_fund"

    def __init__(self, pdf_password: str = ""):
        super().__init__(pdf_password=pdf_password)

    def parse(self, path: Path) -> ParseResult:
        try:
            import casparser
        except ImportError:
            logger.error("casparser not installed. Run: pip install casparser")
            return ParseResult(institution=self.institution, account_type=self.account_type)

        try:
            data = casparser.read_cas_pdf(str(path), self.pdf_password)
        except Exception as exc:
            logger.error("casparser failed on %s: %s", path.name, exc)
            return ParseResult(institution=self.institution, account_type=self.account_type)

        holdings:     list[ParsedHolding]     = []
        transactions: list[ParsedTransaction] = []
        snap_date = date.today()

        # casparser returns a dict with 'folios' key
        for folio in (data.get("folios") or []):
            amc   = folio.get("amc", "")
            for scheme in (folio.get("schemes") or []):
                scheme_name = scheme.get("scheme", "Unknown Fund")
                isin        = scheme.get("isin", "") or scheme.get("isin_div_payout_isin", "")
                amfi_code   = str(scheme.get("amfi", "") or "")
                units       = self._to_decimal(scheme.get("close_calculated") or
                                               scheme.get("close"))
                nav         = self._to_decimal(scheme.get("nav"))
                avg_nav     = self._to_decimal(scheme.get("avg_nav"))
                invested    = self._to_decimal(scheme.get("cost_value"))
                cur_val     = (units * nav) if units and nav else None
                gain_loss   = ((cur_val - invested) if cur_val and invested else None)
                gain_pct    = ((gain_loss / invested * 100) if gain_loss and invested and invested != 0
                               else None)

                if units and units > 0:
                    holdings.append(ParsedHolding(
                        instrument_name=scheme_name,
                        isin=isin or None,
                        symbol=amfi_code or None,
                        units=units,
                        avg_cost_nav=avg_nav,
                        current_nav=nav,
                        current_value=cur_val or Decimal(0),
                        invested_value=invested,
                        gain_loss=gain_loss,
                        gain_loss_pct=gain_pct,
                        holding_type="mutual_fund",
                    ))

                # Per-scheme transactions
                for txn in (scheme.get("transactions") or []):
                    txn_date = txn.get("date")
                    if isinstance(txn_date, str):
                        from datetime import datetime
                        for fmt in ["%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y"]:
                            try:
                                txn_date = datetime.strptime(txn_date, fmt).date()
                                break
                            except ValueError:
                                pass

                    amount  = self._to_decimal(txn.get("amount"))
                    txn_units = self._to_decimal(txn.get("units"))
                    txn_nav   = self._to_decimal(txn.get("nav"))
                    desc      = str(txn.get("description", "")).strip()
                    txn_type  = "debit" if (txn_units or 0) < 0 else "credit"

                    if amount and txn_date:
                        transactions.append(ParsedTransaction(
                            date=txn_date,
                            amount=abs(amount),
                            transaction_type=txn_type,
                            description=f"{scheme_name} — {desc}",
                            raw_description=desc,
                            counterparty=scheme_name,
                        ))

        if holdings:
            snap_date = holdings[0].current_value and date.today()

        return ParseResult(
            institution=self.institution, account_type=self.account_type,
            holdings=holdings, transactions=transactions,
            source_file=str(path),
        )
