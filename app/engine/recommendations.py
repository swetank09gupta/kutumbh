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
from app.knowledge import rules
from app.models.schemas import Account, Balance, Goal, ManualAsset, NetWorthSnapshot, Transaction

ZERO = Decimal(0)


@dataclass
class Recommendation:
    priority:    str   # "high" | "medium" | "low"
    category:    str   # "emergency_fund" | "insurance" | "goal" | "expense" | "investment"
    title:       str
    detail:      str
    action:      str
    metrics:     dict = field(default_factory=dict)  # key numbers driving the rec (snapshotted)

    @property
    def fingerprint(self) -> str:
        """Stable identity across snapshots — used to detect persistence."""
        return f"{self.category}|{self.title}"


def generate(db: Session, owner_id: str = "family") -> list[Recommendation]:
    recs: list[Recommendation] = []

    recs += _emergency_fund_check(db, owner_id)
    recs += _high_expense_check(db, owner_id)
    recs += _speculative_holdings_check(db)
    recs += _emi_burden_check(db, owner_id)
    recs += _ppf_nudge(db, owner_id)
    recs += _80c_dead_weight_check(db, owner_id)
    recs += _retirement_target_sanity_check(db, owner_id)

    # Sort: high first
    priority_order = {"high": 0, "medium": 1, "low": 2}
    return sorted(recs, key=lambda r: priority_order.get(r.priority, 3))


# ─────────────────────────────────────────────────────────────────────────────
def _owner_clause(owner_id: str) -> tuple[str, dict]:
    if owner_id and owner_id != "family":
        return " AND owner_id = :owner", {"owner": owner_id}
    return "", {}


def _monthly_expenses(db: Session, owner_id: str, months: int = 3) -> Decimal:
    """Average monthly spending over the last `months` full months — the shared `expense` definition."""
    from datetime import timedelta, date as dt
    from sqlalchemy import text
    start = dt.today().replace(day=1)
    for _ in range(months):
        start = (start - timedelta(days=1)).replace(day=1)
    clause, params = _owner_clause(owner_id)
    total = db.execute(text(
        "SELECT COALESCE(SUM(amount), 0) FROM v_cashflow WHERE flow = 'expense' "
        f"AND date >= :start AND date < date_trunc('month', CURRENT_DATE){clause}"),
        {"start": start, **params}).scalar() or 0
    return Decimal(str(total)) / months


def _latest_networth(db: Session, owner_id: str) -> NetWorthSnapshot | None:
    return (
        db.query(NetWorthSnapshot)
        .filter_by(owner_id=owner_id)
        .order_by(NetWorthSnapshot.snapshot_date.desc())
        .first()
    )


# ─────────────────────────────────────────────────────────────────────────────
def _emergency_fund_check(db: Session, owner_id: str) -> list[Recommendation]:
    """Uses the Emergency Fund goal itself (corpus and target), so this card and the Goals page agree."""
    monthly_exp = _monthly_expenses(db, "family")
    goal = (db.query(Goal).filter(Goal.goal_type.in_(["emergency", "emergency_fund"])).first())
    if goal is not None:
        proj = next((p for p in project_all_goals(db) if p.goal_id == str(goal.id)), None)
        liquid = float(goal.current_corpus or 0)
        target = float(proj.inflation_adj_target) if proj is not None else float(goal.target_amount or 0)   # derived: months of spending
    else:
        nw = _latest_networth(db, "family")
        liquid = float((nw.bank_balance if nw else ZERO) or ZERO)
        target = float(monthly_exp) * 6
    if monthly_exp == 0 or target <= 0:
        return []
    months_cover = liquid / float(monthly_exp)
    if liquid >= target and months_cover >= 6:
        return []
    gap = max(0.0, target - liquid)
    return [Recommendation(
        priority="high" if liquid < target else "medium",
        category="emergency_fund",
        title="Emergency Fund Below Target" if liquid < target else "Emergency Fund Under 6 Months of Spending",
        detail=(f"Liquid savings ₹{liquid:,.0f} cover {months_cover:.1f} months of spending "
                f"(₹{float(monthly_exp):,.0f}/month). Goal target: ₹{target:,.0f}."),
        action=(f"Build it up by ₹{gap:,.0f}. Park in liquid MF or high-yield savings." if gap
                else "Target met, but it covers under 6 months of spending: consider raising the target."),
    )]


def _high_expense_check(db: Session, owner_id: str) -> list[Recommendation]:
    """Flag spending categories that are a large share of the last 3 full months' spending."""
    from sqlalchemy import text
    from datetime import timedelta, date as dt
    start = dt.today().replace(day=1)
    for _ in range(3):
        start = (start - timedelta(days=1)).replace(day=1)
    clause, params = _owner_clause(owner_id)
    rows = db.execute(text(
        "SELECT category, SUM(amount) AS total FROM v_cashflow WHERE flow = 'expense' AND category <> 'emi' "
        f"AND date >= :start AND date < date_trunc('month', CURRENT_DATE){clause} "
        "GROUP BY category ORDER BY 2 DESC LIMIT 5"), {"start": start, **params}).all()
    monthly_exp = _monthly_expenses(db, owner_id)
    if monthly_exp == 0:
        return []
    recs = []
    for cat, total in rows:
        monthly_avg = Decimal(str(total)) / 3
        share = float(monthly_avg / monthly_exp * 100)
        if share > 30 and cat != "other":
            recs.append(Recommendation(
                priority="medium", category="expense",
                title=f"High Spend: {cat.replace('_', ' ').title()}",
                detail=f"{cat.title()} is ₹{monthly_avg:,.0f}/month ({share:.0f}% of spending).",
                action=f"Review {cat} spend; aim to reduce by 10–15%.",
            ))
    return recs


def _speculative_holdings_check(db: Session) -> list[Recommendation]:
    """Holdings so volatile that no goal's horizon covers them (app.planning.matching.speculative): silver, in practice."""
    try:
        from app.planning import matching
        rows = matching.speculative(db)
    except Exception:
        return []
    if not rows:
        return []
    total = sum(r["value"] for r in rows)
    names = ", ".join(sorted({r["name"].split(" - ")[0][:28] for r in rows})[:3])
    return [Recommendation(
        priority="medium", category="investment", title="Money that is a bet, not a plan",
        detail=(f"₹{total / 100000:.1f} L sits in {names}: about {max(r['volatility_pct'] for r in rows)}% a year of volatility, so a loss over a goal's years is not a 1-in-20 event "
                f"for them until roughly {min(r['needs_years'] for r in rows)}+ years. The planner's expected return for silver is gold's (no extra reward for the risk)."),
        action="Treat it as speculative: do not count it toward a goal, do not add to it, and size it so a 35% fall changes nothing in the plan.")]


def _emi_burden_check(db: Session, owner_id: str) -> list[Recommendation]:
    """Regular EMI / income over the last 6 full months, from the shared definitions (prepayments excluded)."""
    from sqlalchemy import text
    from datetime import timedelta, date as dt
    start = dt.today().replace(day=1)
    for _ in range(6):
        start = (start - timedelta(days=1)).replace(day=1)
    clause, params = _owner_clause(owner_id)
    row = db.execute(text(
        "SELECT COALESCE(SUM(amount) FILTER (WHERE flow = 'expense' AND category = 'emi'), 0) AS emi, "
        "COALESCE(SUM(amount) FILTER (WHERE flow = 'income'), 0) AS income FROM v_cashflow "
        f"WHERE date >= :start AND date < date_trunc('month', CURRENT_DATE){clause}"),
        {"start": start, **params}).one()
    monthly_emi, monthly_income = Decimal(str(row.emi)) / 6, Decimal(str(row.income)) / 6
    if monthly_income > 0:
        emi_ratio = float(monthly_emi / monthly_income * 100)
        if emi_ratio > 40:
            return [Recommendation(
                priority="high",
                category="expense",
                title="High EMI Burden",
                detail=(f"EMIs consume {emi_ratio:.0f}% of income "
                        f"(₹{monthly_emi:,.0f}/month vs income ₹{monthly_income:,.0f}). "
                        "Safe limit: <40%."),
                action="Consider prepaying highest-interest loan first. Avoid new loans.",
            )]
    return []


def _ppf_nudge(db: Session, owner_id: str) -> list[Recommendation]:
    """Remind to max out PPF contributions (annual limit from the rules registry)."""
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

    limit = rules.value("ppf.annual_limit")
    if ppf_contrib < limit:
        remaining = limit - ppf_contrib
        return [Recommendation(
            priority="low",
            category="investment",
            title="PPF Annual Limit Not Maxed",
            detail=f"₹{ppf_contrib:,.0f} contributed this FY. Limit: ₹{limit:,.0f}.",
            action=f"Deposit ₹{remaining:,.0f} more before 31 March to max PPF (80C benefit).",
        )]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# New portfolio-quality checks
# ─────────────────────────────────────────────────────────────────────────────

_ELSS_NAME_PATTERNS = (
    "elss", "tax saver", "tax plan", "quant tax", "taxsaver",
    # Zerodha/CDSL abbreviations:
    "elst",       # HDFC ELSTSAVER (HDFC ELSS Tax Saver)
    "etsf",       # MIRAE ETSF / ABSL ETSF (ELSS Tax Saver Fund)
)
def _load_owners_config() -> list[dict]:
    """Read owners block from config/accounts.yml (for tax_regime + epf_monthly)."""
    try:
        import yaml
        from pathlib import Path
        p = Path("config/accounts.yml")
        if not p.exists():
            return []
        data = yaml.safe_load(p.read_text()) or {}
        return data.get("owners", []) or []
    except Exception:
        return []


def _latest_holdings_for_owners(db: Session, owner_ids: list[str]):
    """Wrap holdings_view.latest_holdings for clean import in this module."""
    try:
        from app.engine.holdings_view import latest_holdings
        return latest_holdings(db, owner_ids=owner_ids)
    except Exception:
        return []


def _resolve_owner_ids(db: Session, owner_id: str) -> list[str]:
    from app.models.schemas import Owner
    if owner_id == "family" or not owner_id:
        return [r[0] for r in db.query(Owner.id).filter(Owner.id != "family").all()]
    return [owner_id]


def _annual_lifestyle_expense(db: Session, months: int = 6) -> Decimal:
    """Family annual lifestyle expenses (excludes investments / EMIs / transfers)."""
    from datetime import date as dt, timedelta
    cutoff = dt.today() - timedelta(days=int(30.44 * months))
    total = (
        db.query(func.sum(Transaction.amount))
        .join(Account, Transaction.account_id == Account.id)
        .filter(Account.account_type == "bank")
        .filter(Transaction.transaction_type == "debit")
        .filter(Transaction.category.notin_(
            ["transfer", "investment", "emi", "income", "refund"]
        ))
        .filter(Transaction.date >= cutoff)
        .scalar()
    ) or ZERO
    if months == 0:
        return ZERO
    return (total / Decimal(months)) * Decimal(12)


def _80c_dead_weight_check(db: Session, owner_id: str) -> list[Recommendation]:
    """
    Flag fresh ELSS contributions as dead-weight when 80C is already saturated.

    For each owner on the OLD tax regime:
      80C consumed ≈ PPF this FY + EPF*12 + home-loan principal repayment (this FY).
    If ≥ ₹1.5 L AND there are ELSS holdings present, fresh ELSS SIPs add a
    3-yr lock-in for zero tax benefit — flag them.
    """
    from datetime import date as dt, timedelta
    from app.models.schemas import Owner as _Owner
    # Prefer DB values (UI-editable); fall back to yaml only if DB has no row.
    owners_db = {o.id: o for o in db.query(_Owner).all()}
    owners_yaml = {o.get("id"): o for o in _load_owners_config()}
    def _cfg(oid: str) -> dict:
        o = owners_db.get(oid)
        if o:
            return {
                "tax_regime": getattr(o, "tax_regime", None) or owners_yaml.get(oid, {}).get("tax_regime", "old"),
                "epf_monthly": float(getattr(o, "epf_monthly", 0) or 0) or float(owners_yaml.get(oid, {}).get("epf_monthly", 0) or 0),
                "home_loan_share_pct": float(getattr(o, "home_loan_share_pct", 0) or 0) or float(owners_yaml.get(oid, {}).get("home_loan_share_pct", 0) or 0),
            }
        return owners_yaml.get(oid, {})
    owner_ids = _resolve_owner_ids(db, owner_id)

    # Household home-loan EMI in last 12 months. Co-borrowers split this by
    # `home_loan_share_pct` — only an owner's share contributes to their 80C.
    from datetime import timedelta
    household_emi_12mo = (
        db.query(func.sum(Transaction.amount))
        .filter(Transaction.category == "emi")
        .filter(Transaction.date >= (dt.today() - timedelta(days=365)))
        .scalar()
    ) or ZERO

    # ── Principal repaid this financial year: the loan schedule (app.engine.loan_report), one definition ─
    # EMI principal from the lender's letters and the debits, plus the part-prepayments of the year (the letters state them).
    # Falls back to the 30% rule only when there is no lender letter at all.
    from app.engine import loan_report
    from app.models.schemas import LoanAnchor as _LoanAnchor
    fy_loan = None
    for (loan_owner,) in db.query(_LoanAnchor.owner_id).distinct().all():
        fy_loan = loan_report.fy_split(db, loan_owner, dt.today())
        if fy_loan:
            break
    if fy_loan is not None:
        household_principal_annual = Decimal(str(fy_loan["principal_year"])) + Decimal(str(fy_loan["prepaid"]))
        _principal_source = "schedule"
    else:
        household_principal_annual = household_emi_12mo * Decimal("0.30")
        _principal_source = "rule_of_thumb_30pct"

    fy_start = dt.today().replace(month=4, day=1)
    if dt.today().month < 4:
        fy_start = fy_start.replace(year=fy_start.year - 1)

    recs = []
    for oid in owner_ids:
        cfg = _cfg(oid)
        if (cfg.get("tax_regime") or "old").lower() != "old":
            continue

        # PPF contributions this FY (from transactions tagged ppf_nps)
        ppf_fy = (
            db.query(func.sum(Transaction.amount))
            .join(Account, Transaction.account_id == Account.id)
            .filter(Account.owner_id == oid)
            .filter(Transaction.category == "investment")
            .filter(Transaction.subcategory == "ppf_nps")
            .filter(Transaction.date >= fy_start)
            .scalar()
        ) or ZERO

        # EPF annual = monthly * 12
        epf_annual = Decimal(str(cfg.get("epf_monthly", 0) or 0)) * Decimal(12)

        # Co-borrower's share of household principal repayment this year.
        share_pct = Decimal(str(cfg.get("home_loan_share_pct", 0) or 0))
        home_principal_est = household_principal_annual * share_pct / Decimal(100)

        consumed = ppf_fy + epf_annual + home_principal_est
        cap = rules.value("income_tax.80c_cap")

        # Find ELSS holdings for this owner
        elss_value = Decimal(0)
        for h in _latest_holdings_for_owners(db, [oid]):
            name = (h.instrument_name or "").lower()
            if any(p in name for p in _ELSS_NAME_PATTERNS):
                elss_value += Decimal(str(h.current_value or 0))

        from app.models.schemas import InstrumentTrade
        bought = db.query(InstrumentTrade.instrument_name).filter(InstrumentTrade.owner_id == oid, InstrumentTrade.side == "B",
                                                                    InstrumentTrade.trade_date >= dt.today() - timedelta(days=365)).all()
        buying_elss = any(any(p_ in (n or "").upper() for p_ in _ELSS_NAME_PATTERNS) for (n,) in bought)       # nothing to stop if no ELSS was bought this year
        if consumed >= cap and elss_value > 0 and buying_elss:
            recs.append(Recommendation(
                priority="medium",
                category="investment",
                title=f"ELSS Dead-Weight ({oid.title()})",
                detail=(
                    f"{oid.title()}'s 80C is already saturated "
                    f"(PPF ₹{float(ppf_fy):,.0f} + EPF ₹{float(epf_annual):,.0f} + "
                    f"home-loan principal ~₹{float(home_principal_est):,.0f} = "
                    f"₹{float(consumed):,.0f} vs ₹{float(cap)/100000:g}L cap). "
                    f"ELSS holdings worth ₹{float(elss_value):,.0f} give zero "
                    f"additional tax benefit and carry a 3-yr lock-in."
                ),
                action=(
                    "Stop fresh ELSS SIPs. Redirect to flexi-cap / large-cap "
                    "index funds. Let existing units roll off the lock-in and "
                    "switch them to a non-ELSS fund."
                ),
                metrics={
                    "owner": oid,
                    "ppf_fy": float(ppf_fy),
                    "epf_annual": float(epf_annual),
                    "home_principal_share": float(home_principal_est),
                    "total_80c_consumed": float(consumed),
                    "elss_value": float(elss_value),
                },
            ))
    return recs


def _retirement_target_sanity_check(db: Session, owner_id: str) -> list[Recommendation]:
    """Only when the target in use is YOUR figure (not the derived one): flag it if it is well below what the household's spending,
    inflation and longevity need (docs/PLANNING.md). The derived target is already what the goal uses."""
    proj = next((p for p in project_all_goals(db) if p.goal_type == "retirement"), None)
    if proj is None or proj.ideal_target_today is None or proj.target_source != "typed":
        return []
    if (proj.typed_target_today or 0) >= 0.8 * proj.ideal_target_today:
        return []
    return [Recommendation(
        priority="medium",
        category="goal",
        title="Retirement Target Under-Funded vs What Your Spending Needs",
        detail=(f"Your retirement figure is ₹{proj.typed_target_today:,.0f} (today's money). From your spending, inflation and lifespan the "
                f"planner derives ₹{proj.ideal_target_today:,.0f} for an 80% chance the money lasts."),
        action="Untick 'use my figures' on the goal to plan on the derived target, or raise your own.",
    )]
