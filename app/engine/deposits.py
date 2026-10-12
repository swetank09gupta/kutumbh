"""
Fixed deposits valued from the bank's own numbers.

An SBI statement row gives, for each deposit: the principal (interest already added to it included), the
**interest accrued** since the last addition, and the **maturity value** (all interest to maturity). So

    worth at the statement date = principal + interest accrued
    worth later (cumulative deposit) = that, grown along the contract to the maturity value:
        value(day) = base x (maturity value / base) ^ (days since the statement / days to maturity)

No rate is typed or assumed. A deposit that pays its interest out (maturity value <= base) stays at
principal + interest accrued — its return is the interest credited to savings (measured in
app.engine.portfolio_xirr). Net worth, Investments and Manual Assets all call `accrued_value`, so they cannot
disagree about a deposit.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Optional

_MATURITY = re.compile(r"Maturity value:\s*₹\s*([\d,]+(?:\.\d+)?)", re.I)
_PRINCIPAL_AS_OF = re.compile(r"Principal as of (\d{4}-\d{2}-\d{2})")
ZERO = Decimal(0)


def maturity_value(asset) -> Optional[Decimal]:
    m = _MATURITY.search(getattr(asset, "notes", "") or "")
    return Decimal(m.group(1).replace(",", "")) if m else None


def _anchor_date(asset) -> Optional[date]:
    """The date base_value() is true on: the statement date, or the deposit's open date when the statement prints only
    the opening principal (no accrued column), so growth to maturity starts where the principal is."""
    m = _PRINCIPAL_AS_OF.search(getattr(asset, "notes", "") or "")
    if m and not getattr(asset, "accrued_interest", None):
        return date.fromisoformat(m.group(1))
    d = getattr(asset, "source_statement_date", None)
    if d:
        return d
    lu = getattr(asset, "last_updated", None)
    return lu.date() if lu else None


def base_value(asset) -> Decimal:
    """Principal plus the interest accrued at the statement date."""
    return (asset.current_value or ZERO) + (getattr(asset, "accrued_interest", None) or ZERO)


def is_cumulative(asset) -> bool:
    base, mat = base_value(asset), maturity_value(asset)
    return bool(base and mat and mat > base * Decimal("1.0001") and asset.maturity_date)


def accrued_value(asset, as_of: date, series: dict | None = None) -> Decimal:   # series: kept for callers
    """What the deposit is worth on `as_of`. (A PPF is worth its statement balance: its interest is credited once,
    on 31 March, and not before.)"""
    base = base_value(asset)
    if getattr(asset, "asset_type", None) not in ("fd", "rd"):
        return asset.current_value or ZERO
    if not is_cumulative(asset):
        return base
    start, end, mat = _anchor_date(asset), asset.maturity_date, maturity_value(asset)
    if not start or end <= start or as_of <= start:
        return base
    t = min(1.0, (as_of - start).days / (end - start).days)
    return (base * Decimal(str((float(mat) / float(base)) ** t))).quantize(Decimal("0.01"))


def contract_rate_pct(asset) -> Optional[float]:
    """Annual rate the statement's own numbers imply from its date to maturity (None if interest is paid out)."""
    if not is_cumulative(asset):
        return None
    start, end = _anchor_date(asset), asset.maturity_date
    if not start or end <= start:
        return None
    years = (end - start).days / 365.25
    return round(((float(maturity_value(asset)) / float(base_value(asset))) ** (1 / years) - 1) * 100, 2)


# ── history from the monthly statements (models.DepositSnapshot) ─────────────
def bank_of(name: str | None) -> str:
    """"SBI FD ...6925" -> "SBI", "Axis FD ...5385" -> "Axis": each bank's statements are its own table."""
    return (name or "").split(" ")[0]


def table_date(newest: dict, asset):
    """The date of the newest statement of the bank this deposit is with."""
    return newest.get((asset.owner_id, bank_of(asset.name)))


def deposit_history(db) -> tuple[dict, dict]:
    """({(owner, name): [(as_on, principal, accrued)]}, {(owner, bank): date of that bank's newest FD table seen})."""
    from collections import defaultdict
    from app.models.schemas import DepositSnapshot
    series: dict = defaultdict(list)
    newest: dict = {}
    for r in db.query(DepositSnapshot).order_by(DepositSnapshot.as_on).all():
        series[(r.owner_id, r.name)].append((r.as_on, r.principal, r.accrued))
        if r.asset_type == "fd":
            newest[(r.owner_id, bank_of(r.name))] = max(newest.get((r.owner_id, bank_of(r.name)), r.as_on), r.as_on)
    return series, newest


def is_live(asset, series: dict, newest: dict) -> bool:
    """A deposit the newest statement no longer lists was closed (or broken): it is not worth anything today.
    Deposits we never saw in a statement (entered by hand) are taken as live."""
    if getattr(asset, "asset_type", None) != "fd":
        return True
    seen = series.get((asset.owner_id, asset.name))
    table = table_date(newest, asset)
    if not seen or table is None:
        return True
    return max(d for d, _p, _a in seen) >= table
