"""
KFintech per-folio "Statement of Account" parser.

Sent monthly from kfintech.com for individual folios.
Password = PAN uppercase (same as full CAS).

Format (2-page PDF):
  Page 1:
    - Investor header: Name, Folio, BOID, PAN
    - Portfolio Summary table:  "Portfolio Summary as on DD/MM/YYYY"
        columns (merged into one cell by pdfplumber):
          Scheme Name | Units Allotted | Cost of Investment | NAV | Current Value
    - Detailed Transaction Summary (text):
        "Scheme <name> ISIN No : <ISIN> (Demat)"
        + transaction rows (often just "Opening Balance", no real transactions)
  Page 2: Transaction submission form — ignored

Identifies by: "Statement of Account for Period" header (not a full CAS).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from app.ingestion.parsers.base import (
    BaseParser, ParsedHolding, ParseResult,
)

logger = logging.getLogger(__name__)

# Matches the 4 numeric columns at the END of a portfolio summary data row.
# Units always have 3 dp; cost and value always 2 dp; NAV has variable dp.
_ROW_RE = re.compile(
    r'^(.+?)\s+'             # scheme name (non-greedy, everything before numbers)
    r'(\d[\d,]*\.\d{3})\s+' # Units Allotted  (3 dp)
    r'(\d[\d,]*\.\d{2})\s+' # Cost of Investment (2 dp)
    r'(\d[\d,]*\.\d+)\s+'   # NAV (variable dp)
    r'(\d[\d,]*\.\d{2})$'   # Current Value (2 dp)
)

_ISIN_SCHEME_RE = re.compile(
    r'Scheme\s+(.+?)\s*-?\s*ISIN\s+No\s*:\s*([A-Z]{2}[A-Z0-9]{9}[0-9])',
    re.I,
)
_AS_ON_RE  = re.compile(r'Portfolio Summary as on (\d{2}/\d{2}/\d{4})', re.I)
_FOLIO_RE  = re.compile(r'FOLIO\s*[:\s]+(\S+)', re.I)


def _dec(s: str) -> Optional[Decimal]:
    try:
        return Decimal(s.replace(",", ""))
    except Exception:
        return None


class KfintechFolioParser(BaseParser):
    """Parser for KFintech per-folio monthly account statements."""

    institution  = "KFintech"
    account_type = "mutual_fund"

    def parse(self, path: Path) -> ParseResult:
        with self._open_pdf(path) as pdf:
            full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
            all_tables = [page.extract_tables() or [] for page in pdf.pages]

        account_ref    = self._extract_folio(full_text)
        isin_map       = self._extract_isin_map(full_text)
        holdings       = self._parse_holdings(all_tables, isin_map)
        statement_date = self._extract_statement_date(full_text)

        logger.info(
            "KFintech folio %s: %d holding(s) (folio=%s, as_on=%s)",
            path.name, len(holdings), account_ref, statement_date,
        )
        return ParseResult(
            institution=self.institution,
            account_type=self.account_type,
            account_ref=account_ref,
            holdings=holdings,
            complete_holdings_snapshot=bool(holdings),
            source_file=str(path),
            statement_date=statement_date,
        )

    def _extract_statement_date(self, text: str):
        m = _AS_ON_RE.search(text)
        if m:
            try:
                return datetime.strptime(m.group(1), "%d/%m/%Y").date()
            except ValueError:
                pass
        return None

    def _extract_folio(self, text: str) -> Optional[str]:
        m = _FOLIO_RE.search(text)
        return m.group(1).strip() if m else None

    def _extract_isin_map(self, text: str) -> dict[str, str]:
        """Return {scheme_name_fragment → ISIN} from the transaction summary section."""
        mapping: dict[str, str] = {}
        for m in _ISIN_SCHEME_RE.finditer(text):
            scheme_raw = m.group(1).strip()
            isin       = m.group(2).strip()
            # Normalise: strip trailing punctuation and plan/option suffixes
            scheme_key = re.sub(r'[-–]\s*$', '', scheme_raw).strip()
            mapping[scheme_key] = isin
        return mapping

    def _find_isin(self, scheme_name: str, isin_map: dict[str, str]) -> Optional[str]:
        """Fuzzy-match scheme name to an extracted ISIN."""
        if not isin_map:
            return None
        # Exact or prefix match
        for key, isin in isin_map.items():
            if scheme_name.startswith(key) or key.startswith(scheme_name):
                return isin
        # Longest common prefix fallback
        best_isin, best_len = None, 0
        for key, isin in isin_map.items():
            common = sum(
                1 for a, b in zip(scheme_name.lower(), key.lower()) if a == b
            )
            if common > best_len:
                best_isin, best_len = isin, common
        return best_isin if best_len > 10 else None

    def _parse_holdings(
        self, all_tables: list[list[list]], isin_map: dict[str, str]
    ) -> list[ParsedHolding]:
        holdings: list[ParsedHolding] = []

        for page_tables in all_tables:
            for table in page_tables:
                if not table:
                    continue
                # Identify Portfolio Summary table by its first cell
                header_text = str(table[0][0] or "")
                if "Portfolio Summary" not in header_text:
                    continue

                for row in table[1:]:
                    # Each data row is a single merged cell
                    cell = str(row[0] or "").strip() if row else ""
                    if not cell or cell.startswith("Total:") or "Scheme Name" in cell:
                        continue

                    m = _ROW_RE.match(cell)
                    if not m:
                        logger.debug("KFintech folio: unmatched row: %r", cell[:80])
                        continue

                    scheme_name  = m.group(1).strip()
                    units        = _dec(m.group(2))
                    invested     = _dec(m.group(3))
                    nav          = _dec(m.group(4))
                    current_val  = _dec(m.group(5))

                    if not units or units <= 0:
                        continue

                    isin     = self._find_isin(scheme_name, isin_map)
                    avg_cost = (invested / units) if invested and units else None

                    holdings.append(ParsedHolding(
                        instrument_name=scheme_name,
                        symbol=scheme_name,
                        isin=isin,
                        units=units,
                        avg_cost_nav=avg_cost,
                        current_nav=nav,
                        current_value=current_val or Decimal(0),
                        invested_value=invested,
                        gain_loss=(current_val - invested) if current_val and invested else None,
                        gain_loss_pct=(
                            ((current_val - invested) / invested * 100)
                            if current_val and invested and invested > 0 else None
                        ),
                        holding_type="mutual_fund",
                    ))

        return holdings
