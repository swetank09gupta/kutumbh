"""
Zerodha Coin allotment / redemption email parser.

Coin sends an HTML email for every MF buy and redeem with no attachment.
The body contains a clean, plain-English block per fund:

    <FUND NAME>
    Folio no.: <folio>, NAV: ₹<nav>  Stamp Duty: ₹<duty>
    ₹<gross_amount>  <units> units

Example subject:
    Coin by Zerodha - Allotment report - 05-05-2026
    Coin by Zerodha - Redemption report - 12-04-2026

Each fund block becomes one ParsedTransaction with rupee amount.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from app.ingestion.parsers.base import (
    BaseParser, ParsedTransaction, ParseResult,
)
from app.engine.amfi_isin import lookup_isin as _amfi_lookup_isin

logger = logging.getLogger(__name__)

# Date in subject: "Allotment report - 05-05-2026"
_SUBJECT_DATE_RE = re.compile(r'(\d{2}-\d{2}-\d{4})')

# Pull "₹9999.5  181.098 units" — the gross amount + unit count for one fund.
_AMOUNT_UNITS_RE = re.compile(
    r'₹\s*([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)\s+units?',
    re.IGNORECASE,
)

# Pull the fund name + folio + NAV from the line preceding the amount line.
_FOLIO_NAV_RE = re.compile(
    r'Folio\s+no\.?:\s*([\w/\-]+)\s*,?\s*NAV\s*:\s*₹\s*([\d.]+)',
    re.IGNORECASE,
)

# Older Coin format (2021–2024): one row per allotment, columns separated by spaces.
#   "2025-01-17 MOTILAL OSWAL MIDCAP FUND - DIRECT GROWTH 91061089716 Rs. 29998.5 Rs. 1.5 114.94 260.985"
# Captures: trade_date | fund_name | folio | gross_amount | nav | units
_OLD_ROW_RE = re.compile(
    r'(\d{4}-\d{2}-\d{2})\s+'             # trade date
    r'(.+?)\s+'                           # fund name (non-greedy)
    r'(\d{6,}[/\w]*)\s+'                  # folio (6+ digits, may include /xx suffix)
    r'Rs\.\s*([\d,]+(?:\.\d+)?)\s+'       # gross amount
    r'Rs\.\s*[\d,]+(?:\.\d+)?\s+'         # stamp duty (skip)
    r'([\d,]+(?:\.\d+)?)\s+'              # NAV
    r'([\d,]+(?:\.\d+)?)',                # units
    re.IGNORECASE,
)


class ZerodhaCoinParser(BaseParser):
    institution  = "Zerodha Coin"
    account_type = "mutual_fund"

    def parse(self, path: Path) -> ParseResult:
        try:
            html = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.error("Coin email read error %s: %s", path.name, exc)
            return ParseResult(institution=self.institution, account_type=self.account_type)

        # Strip script + style + tags → plain text
        text = re.sub(r'<style.*?</style>', ' ', html, flags=re.S | re.I)
        text = re.sub(r'<script.*?</script>', ' ', text, flags=re.S | re.I)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'&nbsp;|&#160;', ' ', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&rupee;|&#8377;', '₹', text)
        text = re.sub(r'\s+', ' ', text).strip()

        # Determine direction. "Allotment success" = buy (debit cashflow).
        # "Redemption" / "Redeem success" = sell (credit cashflow).
        upper = text.upper()
        if "REDEMPTION" in upper or "REDEEM" in upper:
            txn_type = "credit"
            kind     = "REDEEM"
        elif "ALLOTMENT" in upper:
            txn_type = "debit"
            kind     = "BUY"
        else:
            logger.debug("Coin email %s: not an allotment/redemption", path.name)
            return ParseResult(institution=self.institution, account_type=self.account_type,
                               source_file=str(path))

        # Trade date — prefer the date in the email's filename prefix
        # ("Tue,_06_May_..."), else the date in the subject line / body.
        trade_date = self._extract_date_from_filename(path) or self._extract_date_from_text(text)
        if trade_date is None:
            logger.warning("Coin email %s: cannot determine trade date", path.name)
            return ParseResult(institution=self.institution, account_type=self.account_type,
                               source_file=str(path))

        # ── Old format (2021–early 2024): tabular rows in body ───────────────
        # If old-format rows are present, prefer them — each row is fully
        # self-describing (date + fund + folio + amount + nav + units).
        old_rows = list(_OLD_ROW_RE.finditer(text))
        if old_rows:
            transactions: list[ParsedTransaction] = []
            for m in old_rows:
                row_date = self._parse_iso_date(m.group(1))
                fund     = m.group(2).strip()
                folio    = m.group(3)
                amt      = self._to_decimal(m.group(4))
                nav      = m.group(5)
                units    = self._to_decimal(m.group(6))
                if not amt or amt <= 0 or row_date is None:
                    continue
                desc = f"{kind} {units} units of {fund} @ NAV ₹{nav}"
                transactions.append(ParsedTransaction(
                    date=row_date,
                    amount=amt,
                    transaction_type=txn_type,
                    description=desc[:200],
                    raw_description=fund,
                    counterparty=fund or None,
                    reference_number=folio,
                    isin=_amfi_lookup_isin(fund),
                ))
            if transactions:
                logger.info(
                    "Coin %s: %d %s rows (old format)", path.name, len(transactions), kind,
                )
                return ParseResult(
                    institution=self.institution, account_type=self.account_type,
                    transactions=transactions, source_file=str(path),
                )

        # ── New format (mid-2024+): per-fund Folio/NAV + ₹amount/units blocks ─
        folio_navs = list(_FOLIO_NAV_RE.finditer(text))
        amount_units = list(_AMOUNT_UNITS_RE.finditer(text))

        # Stamp duty appears with its own ₹ — exclude those by skipping any
        # ₹X match that's not followed (within ~30 chars) by a "units" token.
        # _AMOUNT_UNITS_RE already requires "units" so the regex itself filters.

        transactions: list[ParsedTransaction] = []
        for i, m_amt in enumerate(amount_units):
            amt   = self._to_decimal(m_amt.group(1))
            units = self._to_decimal(m_amt.group(2))
            if not amt or amt <= 0:
                continue

            # Folio/NAV line preceding this amount block (closest match before it).
            folio = nav = None
            fund_name = ""
            for m_fn in folio_navs:
                if m_fn.start() < m_amt.start():
                    folio = m_fn.group(1)
                    nav   = m_fn.group(2)
                    # Fund name = the text between previous block end and this folio match.
                    name_window = text[max(0, m_fn.start() - 200): m_fn.start()].strip()
                    # Take the trailing chunk that looks like a fund name (caps + dashes).
                    name_chunks = re.findall(r'([A-Z][A-Z0-9 &\-/().]{6,}(?:GROWTH|DIVIDEND|PLAN|FUND|ETF|IDCW))',
                                             name_window)
                    if name_chunks:
                        fund_name = name_chunks[-1].strip()
                else:
                    break

            label = fund_name or (f"Folio {folio}" if folio else "Unknown Fund")
            desc = f"{kind} {units} units of {label}" + (f" @ NAV ₹{nav}" if nav else "")

            transactions.append(ParsedTransaction(
                date=trade_date,
                amount=amt,
                transaction_type=txn_type,
                description=desc[:200],
                raw_description=label,
                counterparty=label or None,
                reference_number=folio,
                isin=_amfi_lookup_isin(fund_name) if fund_name else None,
            ))

        if transactions:
            logger.info(
                "Coin %s: %d %s transactions on %s (%s)",
                path.name, len(transactions), kind, trade_date,
                ", ".join(f"₹{t.amount}" for t in transactions[:3]),
            )

        return ParseResult(
            institution=self.institution, account_type=self.account_type,
            transactions=transactions, source_file=str(path),
        )

    @staticmethod
    def _parse_iso_date(s: str) -> date | None:
        try:
            return datetime.strptime(s.strip(), "%Y-%m-%d").date()
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _extract_date_from_filename(path: Path) -> date | None:
        # Filenames look like "Tue,_06_May_coin_<id>.html"
        m = re.match(r'\w+,_(\d{2})_(\w{3})', path.name)
        if not m:
            return None
        for fmt in ["%d-%b-%Y", "%d-%b-%y"]:
            try:
                # Year is missing in filename — try current year, else current-1.
                today = date.today()
                for yr in (today.year, today.year - 1, today.year - 2):
                    raw = f"{m.group(1)}-{m.group(2)}-{yr}"
                    try:
                        d = datetime.strptime(raw, fmt).date()
                        if d <= today:
                            return d
                    except ValueError:
                        continue
            except Exception:
                pass
        return None

    @staticmethod
    def _extract_date_from_text(text: str) -> date | None:
        m = _SUBJECT_DATE_RE.search(text)
        if not m:
            return None
        for fmt in ["%d-%m-%Y", "%d-%m-%y"]:
            try:
                return datetime.strptime(m.group(1), fmt).date()
            except ValueError:
                continue
        return None
