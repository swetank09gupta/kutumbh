"""
Sovereign Gold Bond terms: the issue date and RBI issue price of a tranche.

SGBs are subscribed through a bank or the RBI, not the broker, so no contract note carries their
cost. The tranche calendar does: the price per gram at issue (online price, after the Rs.50
discount) and the issue date are published, so units held since issue have a known cost.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Optional

# ISIN -> (issue date, redemption date, issue price per gram in Rs)
SGB_TRANCHES: dict[str, tuple[date, date, float]] = {
    "IN0020210178": (date(2021, 11, 2),  date(2029, 11, 2),  4761.0),   # 2021-22 Series VII
    "IN0020220110": (date(2022, 12, 27), date(2030, 12, 27), 5409.0),   # 2022-23 Series III
    "IN0020230069": (date(2023, 9, 14),  date(2031, 9, 14),  5873.0),   # 2023-24 Series II
}


def issue_date(isin: str, instrument_name: str = "") -> Optional[date]:
    if isin in SGB_TRANCHES:
        return SGB_TRANCHES[isin][0]
    m = re.search(r"\bSGB\b.*?\b(20\d{2})\b", instrument_name or "", re.IGNORECASE)
    return date(int(m.group(1)) - 8, 6, 1) if m else None


def issue_price(isin: str) -> Optional[float]:
    return SGB_TRANCHES[isin][2] if isin in SGB_TRANCHES else None


def issue_terms(isin: str) -> Optional[tuple[date, Decimal]]:
    """(issue date, price per unit) when both are published for this tranche."""
    t = SGB_TRANCHES.get(isin)
    return (t[0], Decimal(str(t[2]))) if t else None
