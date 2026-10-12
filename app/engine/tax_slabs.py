"""
Slab tax on ordinary income for one regime and financial year, from app/knowledge/tax_slabs.yml. Pure functions.

Cess is 4% of the tax after rebate. Surcharge (income above ₹50 lakh) is not modelled. A year with no verified entry gives no
estimate (None), never a borrowed one.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import yaml

D = Decimal
PATH = Path(__file__).resolve().parents[1] / "knowledge" / "tax_slabs.yml"
CESS_PCT = D(4)
STALE_AFTER_DAYS = 120


@lru_cache(maxsize=1)
def years() -> dict[str, dict]:
    return {y["fy"]: y for y in yaml.safe_load(PATH.read_text())["years"]}


def regime_rules(fy: str, regime: str) -> dict | None:
    y = years().get(fy)
    return y[regime] if y and regime in y else None


def slab_tax(taxable: Decimal, fy: str, regime: str) -> dict | None:
    """{tax_before_rebate, rebate, tax_after_rebate, cess, total} on ordinary taxable income, or None when the year is not known."""
    r = regime_rules(fy, regime)
    if r is None:
        return None
    taxable = max(D(taxable), D(0))
    tax, lower = D(0), D(0)
    for upper, rate in r["slabs"]:
        top = taxable if upper is None else min(taxable, D(upper))
        if top > lower:
            tax += (top - lower) * D(rate) / 100
        if upper is None or taxable <= D(upper):
            break
        lower = D(upper)
    rebate = min(tax, D(r["rebate"]["max"])) if taxable <= D(r["rebate"]["limit"]) else D(0)
    after = tax - rebate
    cess = after * CESS_PCT / 100
    return {"tax_before_rebate": tax, "rebate": rebate, "tax_after_rebate": after, "cess": cess, "total": after + cess}


def standard_deduction(fy: str, regime: str) -> Decimal | None:
    r = regime_rules(fy, regime)
    return D(r["standard_deduction"]) if r else None


def stale(today: date | None = None) -> str | None:
    """A message when the current financial year has no verified entry, or its entry was verified long ago."""
    today = today or date.today()
    y = today.year if today.month >= 4 else today.year - 1
    fy = f"{y}-{str(y + 1)[2:]}"
    e = years().get(fy)
    if e is None:
        return f"no verified income-tax slabs for FY {fy}: read this year's Budget and add it to app/knowledge/tax_slabs.yml"
    if (today - e["verified_on"]).days > STALE_AFTER_DAYS:
        return f"the FY {fy} slabs were last verified {(today - e['verified_on']).days} days ago"
    return None
