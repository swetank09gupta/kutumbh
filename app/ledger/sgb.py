"""
Sovereign Gold Bond terms: the issue date, maturity and RBI issue price of a tranche (app/knowledge/sgb_tranches.yml).

SGBs are subscribed through a bank or the RBI, not the broker, so no contract note carries their cost. The tranche
calendar does: the nominal price per gram at issue and the issue date are published. Online applicants paid Rs.50 a
gram less (confirmed by a bank debit: Series III cost exactly nominal - 50); app.ledger.sgb_evidence finds the subscription
debit when the bank statement has it, and only without that evidence do units cost the ONLINE price (nominal - 50), the
way almost every holder subscribes. The coupon is always on the nominal price.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

PATH = Path(__file__).resolve().parents[1] / "knowledge" / "sgb_tranches.yml"
COUPON_PCT_PER_HALF_YEAR = Decimal("1.25")           # 2.5% a year on the nominal price, paid every six months


@dataclass(frozen=True)
class Tranche:
    isin: str
    series: str
    issue_date: date
    maturity_date: date
    nominal: Decimal
    online: Decimal
    source: str
    verified_on: date

    def coupon(self, units: Decimal) -> Decimal:
        return (Decimal(units) * self.nominal * COUPON_PCT_PER_HALF_YEAR / 100).quantize(Decimal("0.01"))


@lru_cache(maxsize=1)
def tranches() -> dict[str, Tranche]:
    out = {}
    for e in yaml.safe_load(PATH.read_text())["tranches"]:
        out[e["isin"]] = Tranche(e["isin"], e["series"], e["issue_date"], e["maturity_date"], Decimal(str(e["nominal"])),
                                 Decimal(str(e["online"])), e["source"], e["verified_on"])
    return out


def tranche(isin: str | None) -> Optional[Tranche]:
    return tranches().get(isin or "")


def issue_date(isin: str, instrument_name: str = "") -> Optional[date]:
    t = tranche(isin)
    return t.issue_date if t else None            # never guessed from the name: unknown tranche, unknown date


def issue_price(isin: str) -> Optional[float]:
    t = tranche(isin)
    return float(t.nominal) if t else None


def issue_terms(isin: str) -> Optional[tuple[date, Decimal]]:
    """(issue date, assumed price paid per unit): the online price, when no subscription debit proves what was paid."""
    t = tranche(isin)
    return (t.issue_date, t.online) if t else None
