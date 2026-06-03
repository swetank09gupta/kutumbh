"""
Recommendation engine for family financial planning.
Analyses current financial state and generates actionable recommendations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.engine.goals import project_all_goals
from app.models.schemas import Account, Balance, Goal, ManualAsset, NetWorthSnapshot, Transaction

ZERO = Decimal(0)


@dataclass
class Recommendation:
    priority:    str   # "high" | "medium" | "low"
    category:    str   # "emergency_fund" | "insurance" | "goal" | "expense" | "investment"
    title:       str
    detail:      str
    action:      str


def generate(db: Session, owner_id: str = "family") -> list[Recommendation]:
    recs: list[Recommendation] = []

    recs += _emergency_fund_check(db, owner_id)
    recs += _goal_shortfall_check(db, owner_id)
    recs += _high_expense_check(db, owner_id)
    recs += _asset_allocation_check(db, owner_id)
    recs += _emi_burden_check(db, owner_id)
    recs += _ppf_nudge(db, owner_id)

    # Sort: high first
    priority_order = {"high": 0, "medium": 1, "low": 2}
    return sorted(recs, key=lambda r: priority_order.get(r.priority, 3))


# ─────────────────────────────────────────────────────────────────────────────
def _monthly_expenses(db: Session, owner_id: str, months: int = 3) -> Decimal:
    """Average monthly household expenses over last `months` months."""
    from datetime import timedelta, date as dt
    cutoff = dt.today().replace(day=1)
    # Go back `months` months
    for _ in range(months):
        cutoff = (cutoff - timedelta(days=1)).replace(day=1)

    result = (
        db.query(func.sum(Transaction.amount))
        .join(Account, Transaction.account_id == Account.id)
        .filter(Account.owner_id == owner_id if owner_id != "family"
                else True)
        .filter(Transaction.transaction_type == "debit")
        .filter(Transaction.category.notin_(["transfer", "investment"]))
        .filter(Transaction.date >= cutoff)
        .scalar()
    ) or ZERO

    return result / months


def _latest_networth(db: Session, owner_id: str) -> NetWorthSnapshot | None:
    return (
        db.query(NetWorthSnapshot)
        .filter_by(owner_id=owner_id)
        .order_by(NetWorthSnapshot.snapshot_date.desc())
        .first()
    )


# ─────────────────────────────────────────────────────────────────────────────
def _emergency_fund_check(db: Session, owner_id: str) -> list[Recommendation]:
    monthly_exp = _monthly_expenses(db, owner_id)
    if monthly_exp == 0:
        return []

    target_6m = monthly_exp * 6
    nw = _latest_networth(db, "family")
    liquid = (nw.bank_balance if nw else ZERO) or ZERO

    if liquid < target_6m:
        gap = target_6m - liquid
        return [Recommendation(
            priority="high",
            category="emergency_fund",
            title="Emergency Fund Below 6 Months",
            detail=(f"Current liquid savings ₹{liquid:,.0f} covers "
                    f"{int(liquid/monthly_exp)} months. Target: 6 months (₹{target_6m:,.0f})."),
            action=f"Build emergency fund by ₹{gap:,.0f}. Park in liquid MF or high-yield savings.",
        )]
    return []


def _goal_shortfall_check(db: Session, owner_id: str) -> list[Recommendation]:
    recs = []
    projections = project_all_goals(db, owner_id)
    for p in projections:
        if not p.on_track and p.months_to_goal > 0:
            extra = p.monthly_sip_needed - p.current_monthly_sip
            recs.append(Recommendation(
                priority="high" if p.months_to_goal < 60 else "medium",
                category="goal",
                title=f"Goal at Risk: {p.name}",
                detail=(f"Need ₹{p.inflation_adj_target:,.0f} by {p.target_date}. "
                        f"Projected: ₹{p.projected_corpus:,.0f} "
                        f"(gap ₹{abs(p.gap):,.0f})."),
                action=(f"Increase monthly SIP by ₹{extra:,.0f} "
                        f"(total SIP needed: ₹{p.monthly_sip_needed:,.0f}/month)."),
            ))
    return recs


def _high_expense_check(db: Session, owner_id: str) -> list[Recommendation]:
    """Flag categories that are unusually high vs last 3-month average."""
    from datetime import date as dt, timedelta
    recs = []
    cutoff = dt.today() - timedelta(days=90)

    rows = (
        db.query(Transaction.category, func.sum(Transaction.amount).label("total"))
        .join(Account, Transaction.account_id == Account.id)
        .filter(Transaction.transaction_type == "debit")
        .filter(Transaction.category.notin_(["transfer", "investment", "emi"]))
        .filter(Transaction.date >= cutoff)
        .group_by(Transaction.category)
        .order_by(func.sum(Transaction.amount).desc())
        .limit(5)
        .all()
    )

    monthly_exp = _monthly_expenses(db, owner_id)
    if monthly_exp == 0:
        return []

    for cat, total in rows:
        monthly_avg = total / 3
        share = float(monthly_avg / monthly_exp * 100) if monthly_exp else 0
        if share > 30:
            recs.append(Recommendation(
                priority="medium",
                category="expense",
                title=f"High Spend: {cat.replace('_', ' ').title()}",
                detail=f"{cat.title()} is ₹{monthly_avg:,.0f}/month ({share:.0f}% of expenses).",
                action=f"Review {cat} spend; aim to reduce by 10–15%.",
            ))
    return recs


def _asset_allocation_check(db: Session, owner_id: str) -> list[Recommendation]:
    nw = _latest_networth(db, "family")
    if not nw or not nw.total_assets or nw.total_assets == 0:
        return []

    equity_pct = float((nw.equity_value + nw.mf_value) / nw.total_assets * 100)
    debt_pct   = float((nw.ppf_value + nw.fd_value) / nw.total_assets * 100)

    recs = []
    if equity_pct < 40:
        recs.append(Recommendation(
            priority="medium",
            category="investment",
            title="Low Equity Allocation",
            detail=f"Equity (stocks+MF) is {equity_pct:.0f}% of portfolio. Ideal: 50–70% for long-term goals.",
            action="Increase equity SIPs gradually. Consider index funds for low-cost exposure.",
        ))
    if debt_pct > 60:
        recs.append(Recommendation(
            priority="low",
            category="investment",
            title="Heavy Debt Allocation",
            detail=f"Debt instruments (PPF+FD) are {debt_pct:.0f}% of portfolio.",
            action="Rebalance: shift some FD maturity proceeds to equity mutual funds.",
        ))
    return recs


def _emi_burden_check(db: Session, owner_id: str) -> list[Recommendation]:
    from datetime import date as dt, timedelta
    cutoff = dt.today() - timedelta(days=90)

    emi_total = (
        db.query(func.sum(Transaction.amount))
        .join(Account, Transaction.account_id == Account.id)
        .filter(Transaction.category == "emi")
        .filter(Transaction.date >= cutoff)
        .scalar()
    ) or ZERO
    monthly_emi = emi_total / 3

    monthly_income = (
        db.query(func.sum(Transaction.amount))
        .join(Account, Transaction.account_id == Account.id)
        .filter(Transaction.category == "income")
        .filter(Transaction.transaction_type == "credit")
        .filter(Transaction.date >= cutoff)
        .scalar()
    ) or ZERO
    monthly_income_avg = monthly_income / 3

    if monthly_income_avg > 0:
        emi_ratio = float(monthly_emi / monthly_income_avg * 100)
        if emi_ratio > 40:
            return [Recommendation(
                priority="high",
                category="expense",
                title="High EMI Burden",
                detail=(f"EMIs consume {emi_ratio:.0f}% of income "
                        f"(₹{monthly_emi:,.0f}/month vs income ₹{monthly_income_avg:,.0f}). "
                        "Safe limit: <40%."),
                action="Consider prepaying highest-interest loan first. Avoid new loans.",
            )]
    return []


def _ppf_nudge(db: Session, owner_id: str) -> list[Recommendation]:
    """Remind to max out PPF contributions (₹1.5L/year limit)."""
    ppf_assets = (
        db.query(ManualAsset)
        .filter(ManualAsset.asset_type == "ppf")
        .all()
    )
    if not ppf_assets:
        return [Recommendation(
            priority="low",
            category="investment",
            title="PPF Not Tracked",
            detail="PPF balance not found. Add it via the manual assets API.",
            action="Go to /api/manual/assets and add your PPF accounts.",
        )]

    current_year_start = date.today().replace(month=4, day=1)
    if date.today().month < 4:
        current_year_start = current_year_start.replace(year=current_year_start.year - 1)

    # Check PPF contributions this financial year from bank transactions
    ppf_contrib = (
        db.query(func.sum(Transaction.amount))
        .filter(Transaction.category == "investment")
        .filter(Transaction.subcategory == "ppf_nps")
        .filter(Transaction.date >= current_year_start)
        .scalar()
    ) or ZERO

    limit = Decimal("150000")
    if ppf_contrib < limit:
        remaining = limit - ppf_contrib
        return [Recommendation(
            priority="low",
            category="investment",
            title="PPF Annual Limit Not Maxed",
            detail=f"₹{ppf_contrib:,.0f} contributed this FY. Limit: ₹1,50,000.",
            action=f"Deposit ₹{remaining:,.0f} more before 31 March to max PPF (80C benefit).",
        )]
    return []
