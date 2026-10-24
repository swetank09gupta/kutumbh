"""
EPF (Employees' Provident Fund): a balance you share now and then, rolled forward by the app (docs/PLANNING.md).

The EPFO portal needs a captcha login, so nothing can fetch it. What is shared (config/accounts.yml → `epf`: balance on a date and this month's
contribution) is kept as a Manual Asset; its value on any day is that balance plus the contributions since, plus interest at the declared rate. EPFO adds interest
monthly on the running balance and credits it on 31 March, so within a year the interest is simple and each March it joins the balance. Contributions are assumed
to stay as they are (a raise is not known until you share it); the rate is the latest declared one (docs/INSTRUMENTS.md: `epf.interest_rate`).
Interest on an employee contribution above ₹2.5 lakh a year is taxable; this is not modelled in the value, only noted in the plan.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from app.knowledge import rules

logger = logging.getLogger(__name__)
STALE_DAYS = 400                      # an anchor older than a year and a bit is flagged: share a newer balance


def _month_ends(start: date, end: date):
    """Last day of each month in (start, end]."""
    y, m = start.year, start.month
    while True:
        m += 1
        if m > 12:
            y, m = y + 1, 1
        nxt = date(y + (m == 12), (m % 12) + 1, 1)
        last = date.fromordinal(nxt.toordinal() - 1)
        if last > end:
            return
        yield last


def rolled_value(balance: float, as_of: date, monthly: float, today: date, rate_pct: float | None = None) -> float:
    """Balance on `today`: contributions at each month end, interest accrued monthly on the running balance and added every 31 March."""
    bal, accrued = float(balance), 0.0
    for me in _month_ends(as_of, today):
        r = (rate_pct if rate_pct is not None else float(rules.value("epf.interest_rate", me))) / 100
        accrued += bal * r / 12
        bal += monthly
        if me.month == 3:
            bal, accrued = bal + accrued, 0.0
    return round(bal + accrued, 2)


def value_of(asset, on: date) -> Decimal:
    """A Manual Asset of type 'epf' (or 'nps', contributions only: its NAV is not known) on `on` (the anchor itself on or before its date)."""
    base = Decimal(str(asset.current_value or 0))
    as_of = asset.source_statement_date
    if not as_of or on <= as_of:
        return base
    rate = 0.0 if asset.asset_type == "nps" else (float(asset.interest_rate) if asset.interest_rate else None)      # NPS: contributions only, no NAV is known
    return Decimal(str(rolled_value(float(base), as_of, float(asset.monthly_payment or 0), on, rate)))


def _changed(a, as_of: date, balance: float, monthly: float) -> bool:
    """A newer anchor replaces the stored one; the same date takes a corrected balance or contribution from the config; an older one is ignored."""
    if a.source_statement_date is None or as_of > a.source_statement_date:
        return True
    return as_of == a.source_statement_date and (Decimal(str(balance)) != (a.current_value or 0) or Decimal(str(monthly)) != (a.monthly_payment or 0))


def sync(db, today: date | None = None, entries: list[dict] | None = None, nps: list[dict] | None = None) -> int:
    """Create or refresh the EPF (and NPS) Manual Asset of each configured entry. A newer `as_of` in the config replaces the anchor; an older one is ignored."""
    from app.models.schemas import ManualAsset
    from app.planning import personal
    n = 0
    for e in entries if entries is not None else personal.epf_entries():
        monthly = e["employee_monthly"] + e["employer_monthly"] + e.get("voluntary_monthly", 0.0)      # all that really goes in (the balance grows by it)
        a = db.query(ManualAsset).filter(ManualAsset.owner_id == e["owner"], ManualAsset.asset_type == "epf").first()
        if a is None:
            db.add(ManualAsset(owner_id=e["owner"], asset_type="epf", institution="EPFO", name="EPF (EPFO)", current_value=Decimal(str(e["balance"])),
                               monthly_payment=Decimal(str(monthly)), source_statement_date=e["as_of"], goal_tag="retirement"))
            n += 1
        elif _changed(a, e["as_of"], e["balance"], monthly):
            a.current_value, a.monthly_payment, a.source_statement_date = Decimal(str(e["balance"])), Decimal(str(monthly)), e["as_of"]
            n += 1
    for e in nps if nps is not None else (personal.nps_entries() if entries is None else []):
        a = db.query(ManualAsset).filter(ManualAsset.owner_id == e["owner"], ManualAsset.asset_type == "nps").first()
        if a is None:
            db.add(ManualAsset(owner_id=e["owner"], asset_type="nps", institution="NPS", name="NPS", current_value=Decimal(str(e["balance"])),
                               monthly_payment=Decimal(str(e["monthly"])), source_statement_date=e["as_of"], goal_tag="retirement"))
            n += 1
        elif _changed(a, e["as_of"], e["balance"], e["monthly"]):
            a.current_value, a.monthly_payment, a.source_statement_date = Decimal(str(e["balance"])), Decimal(str(e["monthly"])), e["as_of"]
            n += 1
    if n:
        db.commit()
    return n


def holders(db, owner_ids: list[str] | None = None, today: date | None = None, kind: str = "epf") -> dict[str, dict]:
    """{owner: {balance (rolled to today), monthly (what goes in), as_of, rate_pct}} for every EPF (or `kind="nps"`) asset."""
    from app.models.schemas import ManualAsset
    today = today or date.today()
    q = db.query(ManualAsset).filter(ManualAsset.asset_type == kind)
    if owner_ids is not None:
        q = q.filter(ManualAsset.owner_id.in_(owner_ids))
    out = {}
    for a in q.all():
        out[a.owner_id] = {"balance": float(value_of(a, today)), "monthly": float(a.monthly_payment or 0), "as_of": a.source_statement_date,
                           "rate_pct": (0.0 if kind == "nps" else float(a.interest_rate) if a.interest_rate else float(rules.value("epf.interest_rate", today)))}
    return out


def total_for(db, owner_ids: list[str], today: date | None = None) -> float:
    return sum(h["balance"] for h in holders(db, owner_ids, today).values())


def stale(db, today: date | None = None) -> list[str]:
    """Owners whose shared balance is older than a year (the roll-forward is only an estimate that long)."""
    today = today or date.today()
    return [o for o, h in holders(db, None, today).items() if h["as_of"] and (today - h["as_of"]).days > STALE_DAYS]
