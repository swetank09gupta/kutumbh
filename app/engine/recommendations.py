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
    recs += _goal_shortfall_check(db, owner_id)
    recs += _high_expense_check(db, owner_id)
    recs += _asset_allocation_check(db, owner_id)
    recs += _speculative_holdings_check(db)
    recs += _emi_burden_check(db, owner_id)
    recs += _ppf_nudge(db, owner_id)
    # New portfolio-quality checks (added 2026-06)
    recs += _80c_dead_weight_check(db, owner_id)
    recs += _midcap_concentration_check(db, owner_id)
    recs += _international_gap_check(db, owner_id)
    recs += _direct_equity_fragmentation_check(db, owner_id)
    recs += _retirement_target_sanity_check(db, owner_id)
    recs += _market_timing_recommendations(db, owner_id)

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


def _asset_allocation_check(db: Session, owner_id: str) -> list[Recommendation]:
    """Equity share of *financial* assets: real estate is not something SIPs can rebalance."""
    nw = _latest_networth(db, "family")
    if not nw:
        return []
    financial = (nw.bank_balance + nw.equity_value + nw.mf_value + nw.ppf_value + nw.fd_value
                 + nw.other_assets)
    if not financial or financial == 0:
        return []

    equity_pct = float((nw.equity_value + nw.mf_value) / financial * 100)
    debt_pct   = float((nw.ppf_value + nw.fd_value) / financial * 100)
    rec = _equity_share_for_goals(db, owner_id)            # what the goals' horizons suggest (docs/PLANNING.md), in percent
    lo, hi = max(0.0, rec - 10), rec + 10

    recs = []
    if equity_pct < lo:
        recs.append(Recommendation(
            priority="medium",
            category="investment",
            title="Low Equity Allocation",
            detail=(f"Equity and funds are {equity_pct:.0f}% of financial assets "
                    f"(bank, equity, MF, PPF, FD; real estate excluded). Your goals' horizons suggest about {rec:.0f}% "
                    f"(emergency money 0%, goals 10+ years away 75%, nearer ones less)."),
            action="Increase equity SIPs gradually. Consider index funds for low-cost exposure.",
        ))
    if equity_pct > hi:
        recs.append(Recommendation(
            priority="medium",
            category="investment",
            title="High Equity Allocation for Your Horizons",
            detail=f"Equity and funds are {equity_pct:.0f}% of financial assets; your goals' horizons suggest about {rec:.0f}%.",
            action="Direct new savings to debt/PPF for the nearer goals, or rebalance when a goal is within five years.",
        ))
    if debt_pct > max(60.0, 100 - rec + 20):
        recs.append(Recommendation(
            priority="low",
            category="investment",
            title="Heavy Debt Allocation",
            detail=f"Debt instruments (PPF+FD) are {debt_pct:.0f}% of financial assets.",
            action="Rebalance: shift some FD maturity proceeds to equity mutual funds.",
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


def _equity_share_for_goals(db: Session, owner_id: str) -> float:
    """Percent of equity the goals' horizons call for (app.planning.allocation.goals_equity_share), for the household's risk appetite."""
    from app.models.schemas import Owner
    from app.planning import allocation
    risk = next((o.risk_appetite for o in db.query(Owner).filter(Owner.id == "user")), None)
    items = [(float(p.current_corpus or 0), p.months_to_goal / 12, p.goal_type) for p in project_all_goals(db, owner_id)]
    return round(allocation.goals_equity_share(items, risk) * 100, 1)


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
_MIDCAP_PATTERNS = (
    "midcap", "mid cap", "mid-cap",
    "smallcap", "small cap", "small-cap",
    "large and midcap", "large & midcap", "large&midcap", "large and mid cap",
    # Zerodha/CDSL abbreviations:
    "mid30",      # MOMF MID30F (Motilal Oswal Midcap 30)
    "midop",      # PGIM MIDOP (PGIM India Midcap Opportunities)
    "l&mcf",      # MOMF L&MCF (Motilal Oswal Large & Midcap Fund)
    "lmcf",
    "igmcf",      # NIP IGMCF (Nippon India Growth Midcap Fund)
    "midf",
)
_INTL_PATTERNS      = ("nasdaq", "us bluechip", "us equity", "global", "world",
                       "international", "fang", "s&p 500", "sp 500", "spx",
                       "developed market", "emerging market")


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
    from datetime import date as dt
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

        if consumed >= cap and elss_value > 0:
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


def _midcap_concentration_check(db: Session, owner_id: str) -> list[Recommendation]:
    """
    Flag overweight midcap/smallcap exposure relative to total equity.
    Indian midcap P/E is historically elevated → concentration risk.
    """
    owner_ids = _resolve_owner_ids(db, owner_id)
    holdings = _latest_holdings_for_owners(db, owner_ids)
    if not holdings:
        return []

    equity_total = Decimal(0)
    midcap_total = Decimal(0)
    for h in holdings:
        ht = (h.holding_type or "").lower()
        if ht not in ("mutual_fund", "equity"):
            continue
        v = Decimal(str(h.current_value or 0))
        equity_total += v
        name = (h.instrument_name or "").lower()
        if any(p in name for p in _MIDCAP_PATTERNS):
            midcap_total += v

    if equity_total < Decimal(500000):  # too small to bother
        return []
    pct = float(midcap_total / equity_total * 100)
    if pct < 25:  # 25% pure mid/small is already aggressive
        return []
    return [Recommendation(
        priority="medium",
        category="investment",
        title="Midcap / Smallcap Overweight",
        detail=(
            f"Mid- and small-cap funds are ~{pct:.0f}% of your equity book "
            f"(₹{float(midcap_total):,.0f} of ₹{float(equity_total):,.0f}). "
            f"With Nifty Midcap 150 P/E ~35-40, concentration risk is high."
        ),
        action=(
            "Stop fresh SIPs into pure midcap funds. Redirect to flexi-cap "
            "(PPFAS) or Nifty 50 index. Existing units can ride — selling "
            "triggers LTCG."
        ),
        metrics={
            "midcap_pct": round(pct, 1),
            "midcap_value": float(midcap_total),
            "equity_total": float(equity_total),
        },
    )]


def _international_gap_check(db: Session, owner_id: str) -> list[Recommendation]:
    """Flag missing international equity exposure (US / global)."""
    owner_ids = _resolve_owner_ids(db, owner_id)
    holdings = _latest_holdings_for_owners(db, owner_ids)
    if not holdings:
        return []

    equity_total = Decimal(0)
    intl_total = Decimal(0)
    for h in holdings:
        ht = (h.holding_type or "").lower()
        if ht not in ("mutual_fund", "equity"):
            continue
        v = Decimal(str(h.current_value or 0))
        equity_total += v
        name = (h.instrument_name or "").lower()
        if any(p in name for p in _INTL_PATTERNS):
            intl_total += v

    if equity_total < Decimal(500000):
        return []
    pct = float(intl_total / equity_total * 100) if equity_total else 0
    if pct >= 5:
        return []
    return [Recommendation(
        priority="low",
        category="investment",
        title="No International Equity Exposure",
        detail=(
            f"International equity is {pct:.1f}% of your equity book "
            f"(target: 5–10% for currency + geography diversification)."
        ),
        action=(
            "Start a SIP into Motilal Oswal Nasdaq 100 FOF, ICICI Pru US "
            "Bluechip, or a global index FOF. ₹5–10k/month is a reasonable "
            "starting point."
        ),
        metrics={"intl_pct": round(pct, 2), "intl_value": float(intl_total),
                 "equity_total": float(equity_total)},
    )]


def _direct_equity_fragmentation_check(db: Session, owner_id: str) -> list[Recommendation]:
    """Flag scattered direct equity positions that are hard to manage."""
    owner_ids = _resolve_owner_ids(db, owner_id)
    holdings = _latest_holdings_for_owners(db, owner_ids)
    if not holdings:
        return []

    # Dedupe by ISIN since CDSL + Zerodha mirror the same stock
    seen_isins: dict[str, float] = {}
    for h in holdings:
        if (h.holding_type or "").lower() != "equity":
            continue
        key = h.isin or h.instrument_name or ""
        v = float(h.current_value or 0)
        if key not in seen_isins or v > seen_isins[key]:
            seen_isins[key] = v

    if len(seen_isins) <= 5:
        return []
    total = sum(seen_isins.values())
    if total == 0:
        return []
    avg = total / len(seen_isins)
    if avg >= 150000:  # positions large enough to be intentional
        return []
    return [Recommendation(
        priority="low",
        category="investment",
        title="Scattered Direct Equity Positions",
        detail=(
            f"You hold {len(seen_isins)} direct stocks averaging "
            f"₹{avg:,.0f} per position (total ₹{total:,.0f}). "
            f"Hard to track and rebalance."
        ),
        action=(
            "Pick your top 3 conviction names; exit the rest gradually "
            "(use ₹1.25 L LTCG exemption per FY). Redirect proceeds to a "
            "Nifty 50 index fund for simpler management."
        ),
    )]


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


# ─────────────────────────────────────────────────────────────────────────────
# Market-aware investment timing — combines portfolio gaps with current
# valuations to recommend WHERE (fund category + 2-3 names) + HOW MUCH +
# WHEN (lump-sum / STP / wait).
# ─────────────────────────────────────────────────────────────────────────────

# Suggested fund shortlists per category. Curated — update annually based on
# 5-yr rolling performance + expense ratio. UI surfaces these as starting points.
_FUND_SHORTLIST = {
    "large_cap_index": [
        "UTI Nifty 50 Index Fund - Direct",
        "Nippon India Nifty 50 Index Fund - Direct",
        "ICICI Pru Nifty 50 Index Fund - Direct",
    ],
    "flexi_cap": [
        "PPFAS Flexi Cap Fund - Direct",
        "HDFC Flexi Cap Fund - Direct",
        "Quant Flexi Cap Fund - Direct",
    ],
    "international": [
        "Motilal Oswal Nasdaq 100 FOF - Direct",
        "ICICI Pru US Bluechip Equity - Direct",
        "Mirae Asset NYSE FANG+ ETF FOF - Direct",
    ],
    "debt_long": [
        "Bharat Bond ETF FOF - Direct",
        "Edelweiss CRISIL IBX 50:50 Gilt Plus SDL - Direct",
        "SBI Magnum Constant Maturity Fund - Direct",
    ],
    "gold_etf": [
        "Nippon India Gold BeES",
        "HDFC Gold ETF",
        "ICICI Pru Gold ETF",
    ],
}


def _market_timing_recommendations(db: Session, owner_id: str) -> list[Recommendation]:
    """
    For each portfolio gap (low international, low large-cap, midcap overweight,
    no debt buffer), recommend a specific fund category + ₹ amount + timing
    based on current market valuations.

    - Cheap zone  → lump-sum or aggressive ramp
    - Fair zone   → standard SIP
    - Expensive   → STP over 6-12 months from liquid fund, or wait
    """
    from app.engine.market_signals import get_market_signals

    try:
        sig = get_market_signals()
    except Exception:
        return []

    owner_ids = _resolve_owner_ids(db, owner_id)
    holdings = _latest_holdings_for_owners(db, owner_ids)
    if not holdings:
        return []

    # ── Re-derive same buckets as portfolio-quality checks ──────────────────
    equity_total = midcap_total = intl_total = large_total = Decimal(0)
    for h in holdings:
        ht = (h.holding_type or "").lower()
        if ht not in ("mutual_fund", "equity"):
            continue
        v = Decimal(str(h.current_value or 0))
        equity_total += v
        name = (h.instrument_name or "").lower()
        if any(p in name for p in _MIDCAP_PATTERNS):
            midcap_total += v
        if any(p in name for p in _INTL_PATTERNS):
            intl_total += v
        # crude large-cap proxy: contains "nifty" / "large" / "bluechip" / "lcfd"
        if any(p in name for p in ("nifty 50", "nifty50", " large ", "bluechip",
                                    "lcfd", "large cap", "largecap", "large-cap",
                                    "sensex", "bse 30")):
            large_total += v

    if equity_total < Decimal(300000):
        return []

    midcap_pct = float(midcap_total / equity_total * 100)
    intl_pct   = float(intl_total / equity_total * 100)
    large_pct  = float(large_total / equity_total * 100)

    # ── Market context strings ──────────────────────────────────────────────
    n50_pe = sig.get("nifty50_pe")
    mid_pe = sig.get("nifty_midcap_pe")
    n50_zone = sig.get("nifty50_zone", "unknown")
    mid_zone = sig.get("nifty_midcap_zone", "unknown")
    midcap_premium = sig.get("midcap_premium_pct")

    ctx_large = (
        f"Nifty 50 P/E {n50_pe:.1f} ({n50_zone} vs 5-yr avg "
        f"{_HIST_AVG_PE_LARGE:.0f})"
        if n50_pe else "Nifty 50 P/E unavailable"
    )
    ctx_mid = (
        f"Nifty Midcap 150 P/E {mid_pe:.1f} ({mid_zone}; "
        f"{midcap_premium:+.0f}% premium over Nifty 50)"
        if mid_pe else "Midcap P/E unavailable"
    )

    def _timing(zone: str) -> str:
        return {
            "cheap":     "Lump-sum now (or front-loaded SIP for next 3 months).",
            "fair":      "Standard monthly SIP.",
            "expensive": "STP over 6-12 months from a liquid fund — averages "
                         "out the entry; avoid full lump-sum.",
            "unknown":   "Standard monthly SIP.",
        }.get(zone, "Standard monthly SIP.")

    def _fund_lines(key: str) -> str:
        return " · ".join(_FUND_SHORTLIST.get(key, []))

    recs: list[Recommendation] = []

    # ── 1. Large-cap / index gap ────────────────────────────────────────────
    if large_pct < 15:
        amount = max(5000, int(equity_total / Decimal(100) / 100) * 100)  # ~1% of equity, rounded to ₹100
        recs.append(Recommendation(
            priority="medium",
            category="investment",
            title="Build Large-Cap / Index Exposure",
            detail=(
                f"Large-cap is only {large_pct:.0f}% of your equity book "
                f"(target 25-30%). {ctx_large}."
            ),
            action=(
                f"Start ₹{amount:,}/month into a Nifty 50 index fund. "
                f"Timing: {_timing(n50_zone)} Options: {_fund_lines('large_cap_index')}."
            ),
        ))

    # ── 2. International equity gap ─────────────────────────────────────────
    if intl_pct < 5:
        amount = max(5000, int(equity_total / Decimal(120) / 100) * 100)
        ndx_pe = sig.get("nasdaq100_pe")
        usd_inr = sig.get("usd_inr")
        intl_ctx = []
        if ndx_pe:
            intl_ctx.append(f"Nasdaq 100 P/E {ndx_pe:.0f}")
        if usd_inr:
            intl_ctx.append(f"USD/INR {usd_inr:.1f}")
        intl_blurb = " · ".join(intl_ctx) if intl_ctx else "Diversifies INR exposure."
        recs.append(Recommendation(
            priority="low",
            category="investment",
            title="Add International Equity",
            detail=(
                f"International equity is {intl_pct:.1f}% of your book "
                f"(target 5-10%). {intl_blurb}."
            ),
            action=(
                f"Start ₹{amount:,}/month into a US / global FOF. "
                f"Timing: STP from liquid fund if Nasdaq has run hard; "
                f"else standard SIP. Options: {_fund_lines('international')}."
            ),
        ))

    # ── 3. Midcap overweight — redirect, don't sell ─────────────────────────
    if midcap_pct >= 25:
        recs.append(Recommendation(
            priority="medium",
            category="investment",
            title="Redirect Midcap SIPs to Flexi/Large Cap",
            detail=(
                f"Midcap/smallcap is {midcap_pct:.0f}% of equity. {ctx_mid}. "
                f"Mid/small valuations elevated → pause new mid-cap buys."
            ),
            action=(
                f"Don't sell (LTCG drag). Stop fresh SIPs into pure midcap. "
                f"Redirect to flexi cap or Nifty 50 index. "
                f"Flexi options: {_fund_lines('flexi_cap')}."
            ),
        ))

    # ── 4. Debt / bond buffer when 10y yield is attractive ──────────────────
    yld = sig.get("ind_10y_yield") or 0
    if yld >= 7.25:
        # Suggest only if user lacks dedicated debt MF (FD/PPF count separately)
        debt_mf = sum(
            Decimal(str(h.current_value or 0))
            for h in holdings
            if (h.holding_type or "").lower() == "mutual_fund"
            and any(p in (h.instrument_name or "").lower()
                    for p in ("gilt", "bond", "constant maturity", "long duration",
                              "g-sec", "gsec", "psu debt"))
        )
        if debt_mf < Decimal(200000):
            recs.append(Recommendation(
                priority="low",
                category="investment",
                title="Lock In High G-Sec Yields",
                detail=(
                    f"India 10-yr G-Sec yield is {yld:.2f}% — historically "
                    f"attractive. Long-duration debt funds benefit if rates "
                    f"fall over next 12-18 months."
                ),
                action=(
                    f"Consider ₹50k-1L lump-sum into a long-duration / "
                    f"gilt fund. Options: {_fund_lines('debt_long')}."
                ),
            ))

    return recs


# Helper constants for f-string clarity above
_HIST_AVG_PE_LARGE = 22.0

