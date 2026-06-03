"""
Goal projection engine.
Uses standard financial math (PMT / FV formulas) to calculate:
  - How much SIP is needed each month to hit a goal
  - Projected corpus at target date given current savings rate
  - Inflation-adjusted target amount
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.models.schemas import Goal, NetWorthSnapshot
from app.engine.inflation import get_india_cpi


@dataclass
class GoalProjection:
    goal_id:            str
    name:               str
    goal_type:          str
    target_date:        date
    inflation_adj_target: float   # target in today's ₹ terms
    current_corpus:     float
    monthly_sip_needed: float     # to reach target from today
    projected_corpus:   float     # if current SIP maintained
    gap:                float     # projected_corpus - inflation_adj_target
    months_to_goal:     int
    on_track:           bool
    current_monthly_sip: float    # what they're actually investing (estimated)


def _months_between(start: date, end: date) -> int:
    return max(1, (end.year - start.year) * 12 + (end.month - start.month))


# ── Age-based target date overrides ───────────────────────────────────────────
# Standard Indian financial-planning milestones. Override per child via
# `children:` block in config/accounts.yml.
_DEFAULT_EDUCATION_AGE = 18
_DEFAULT_MARRIAGE_AGE  = 25


def _load_children_from_config() -> list[dict]:
    """
    Read optional children profiles from config/accounts.yml.

    Schema:
      children:
        - id: "child1"
          name: "Aarav"
          dob: "2020-04-15"
          education_age: 18      # optional, default 18
          marriage_age: 25       # optional, default 25
    """
    try:
        import yaml
        from pathlib import Path
        p = Path("config/accounts.yml")
        if not p.exists():
            return []
        data = yaml.safe_load(p.read_text()) or {}
        return data.get("children", []) or []
    except Exception as exc:
        import logging
        logging.getLogger(__name__).debug("children config load failed: %s", exc)
        return []


def _add_years(d: date, years: int) -> date:
    """Add `years` to a date, clamping Feb-29 to Feb-28 when needed."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def _age_target_date(goal: Goal, db: Session) -> date:
    """
    Return the age-implied target_date for a goal, overriding the stored
    target_date when an owner's DOB + retirement_age (for retirement goals)
    or a child's DOB + milestone age (for child_*) is available.

    Falls back to goal.target_date when no profile data exists.
    """
    from app.models.schemas import Owner

    stored = goal.target_date
    gt = (goal.goal_type or "").lower()

    if gt == "retirement":
        owner = db.get(Owner, goal.owner_id) if goal.owner_id else None
        if owner and owner.birth_date and owner.retirement_age:
            try:
                ret_age = int(owner.retirement_age)
                implied = _add_years(owner.birth_date, ret_age)
                return implied
            except (ValueError, TypeError):
                pass
        return stored

    if gt in ("child_education", "child_marriage"):
        children = _load_children_from_config()
        if children:
            # If multiple children, take the OLDEST one (their milestone comes
            # first → drives earliest funding need). Override later by adding
            # one Goal row per child with the child's id in `notes`.
            from datetime import datetime as _dt
            def _parse_dob(c):
                v = c.get("dob")
                if isinstance(v, date):
                    return v
                try:
                    return _dt.strptime(str(v), "%Y-%m-%d").date()
                except Exception:
                    return None
            with_dob = [(c, _parse_dob(c)) for c in children]
            with_dob = [(c, d) for c, d in with_dob if d]
            if with_dob:
                with_dob.sort(key=lambda cd: cd[1])  # oldest first
                child, dob = with_dob[0]
                age = (
                    int(child.get("education_age", _DEFAULT_EDUCATION_AGE))
                    if gt == "child_education"
                    else int(child.get("marriage_age", _DEFAULT_MARRIAGE_AGE))
                )
                return _add_years(dob, age)
        return stored

    return stored


def _future_value_lump_sum(present_value: float, annual_rate: float, months: int) -> float:
    """FV of a lump sum."""
    r = annual_rate / 12 / 100
    return present_value * ((1 + r) ** months)


def _pmt(future_value: float, annual_rate: float, months: int, pv: float = 0) -> float:
    """
    Monthly SIP needed to reach future_value in `months` months,
    given current corpus `pv` and annual return rate `annual_rate`%.
    """
    r = annual_rate / 12 / 100
    if r == 0:
        remaining = future_value - pv
        return max(0, remaining / months)

    fv_of_pv  = _future_value_lump_sum(pv, annual_rate, months)
    remaining = future_value - fv_of_pv
    if remaining <= 0:
        return 0.0

    # PMT = FV * r / ((1+r)^n - 1)
    denominator = ((1 + r) ** months) - 1
    return (remaining * r) / denominator if denominator > 0 else 0.0


def _inflation_adjusted_target(nominal_target: float, inflation_rate: float,
                                years: float) -> float:
    """Target in today's money terms (already inflation-adjusted by user usually)."""
    # If the user set target in today's money, we grow it by inflation to get future value
    return nominal_target * ((1 + inflation_rate / 100) ** years)


def project_goal(goal: Goal, current_monthly_sip: float = 0,
                 db: Session | None = None) -> GoalProjection:
    today      = date.today()
    # Prefer age-implied target_date when owner DOB + retirement_age or child
    # DOB are available — the static target_date in the goal row may be
    # arbitrary or out of sync with the actual life stage.
    effective_target = _age_target_date(goal, db) if db is not None else goal.target_date
    if effective_target is None:
        effective_target = goal.target_date
    months     = _months_between(today, effective_target)
    years      = months / 12

    pv         = float(goal.current_corpus or 0)
    target_nom = float(goal.target_amount or 0)
    ret_rate   = float(goal.expected_return_rate or 12)
    # Use live India CPI if goal still has the default 6% inflation rate
    stored_inf = float(goal.inflation_rate or 0)
    inf_rate   = get_india_cpi() if stored_inf == 6.0 else stored_inf

    # Inflate the target (target is in today's money; we need future ₹)
    inf_adj_target = _inflation_adjusted_target(target_nom, inf_rate, years)

    # SIP needed from today
    sip_needed = _pmt(inf_adj_target, ret_rate, months, pv)

    # Projected corpus if current SIP maintained
    fv_pv  = _future_value_lump_sum(pv, ret_rate, months)
    r      = ret_rate / 12 / 100
    if r > 0 and current_monthly_sip > 0:
        fv_sip = current_monthly_sip * (((1 + r) ** months - 1) / r)
    else:
        fv_sip = current_monthly_sip * months
    projected = fv_pv + fv_sip

    gap      = projected - inf_adj_target
    on_track = gap >= 0

    return GoalProjection(
        goal_id=str(goal.id),
        name=goal.name,
        goal_type=goal.goal_type or "custom",
        target_date=effective_target,
        inflation_adj_target=round(inf_adj_target, 2),
        current_corpus=pv,
        monthly_sip_needed=round(max(0, sip_needed), 2),
        projected_corpus=round(projected, 2),
        gap=round(gap, 2),
        months_to_goal=months,
        on_track=on_track,
        current_monthly_sip=current_monthly_sip,
    )


def refresh_corpus_from_networth(db: Session, owner_id: str) -> int:
    """
    Auto-set current_corpus for each goal from the owner's latest holdings + net worth.

    Priority for equity/MF holdings:
      1. If a holding has goal_tag set, its value goes exclusively to that goal.
      2. Untagged holdings are split proportionally by target_amount across goals of
         the same asset class (retirement vs child goals), preventing double-counting.

    Liquid assets for emergency_fund:
      bank_balance  +  manual_assets where is_liquid=True  (MOD/sweep FDs, liquid bonds)

    Custom goals: corpus is never auto-updated (user manages manually).
    Returns number of goals updated.
    """
    from sqlalchemy import func
    from app.models.schemas import Account, Holding, ManualAsset, Owner

    nw = (
        db.query(NetWorthSnapshot)
        .filter(NetWorthSnapshot.owner_id.in_([owner_id, "family"]))
        .order_by(NetWorthSnapshot.snapshot_date.desc())
        .first()
    )
    if not nw:
        return 0

    bank  = float(nw.bank_balance or 0)
    ppf   = float(nw.ppf_value    or 0)   # family-aggregated; used only for legacy fallback
    fd    = float(nw.fd_value     or 0)
    other = float(nw.other_assets or 0)

    # ── Per-owner latest net-worth snapshots ──────────────────────────────────
    # Used so each owner's retirement goal adds ONLY that owner's PPF, instead
    # of every retirement goal double-counting the family-aggregated total.
    from sqlalchemy import func as _f
    per_owner_nw: dict[str, NetWorthSnapshot] = {}
    sub = (
        db.query(NetWorthSnapshot.owner_id, _f.max(NetWorthSnapshot.snapshot_date).label("d"))
        .group_by(NetWorthSnapshot.owner_id)
        .subquery()
    )
    rows = (
        db.query(NetWorthSnapshot)
        .join(sub, (NetWorthSnapshot.owner_id == sub.c.owner_id) &
                   (NetWorthSnapshot.snapshot_date == sub.c.d))
        .all()
    )
    for r in rows:
        per_owner_nw[r.owner_id] = r

    def _ppf_for(owner: str) -> float:
        nwr = per_owner_nw.get(owner)
        return float(nwr.ppf_value or 0) if nwr else 0.0

    # When owner_id is "family" we want all individual owners, not just rows
    # literally tagged "family" (which don't exist on the accounts table).
    # Expand "family" → every owner id that exists.
    if owner_id == "family":
        all_owner_ids = [r[0] for r in db.query(Owner.id).all()]
    else:
        all_owner_ids = [owner_id]

    # ── Liquid FDs / sweep deposits count toward emergency fund ───────────────
    liquid_extra = float(
        db.query(func.sum(ManualAsset.current_value))
        .filter(
            ManualAsset.owner_id.in_(all_owner_ids),
            ManualAsset.is_liquid == True,         # noqa: E712
            ManualAsset.current_value.isnot(None),
        )
        .scalar() or 0
    )

    # ── Tagged holdings: sourced from the deduped latest-holdings VIEW ────────
    # The view already handles per-account latest snapshot and dedups the same
    # ISIN appearing in multiple accounts (CDSL + Zerodha mirror each other).
    # Earlier this used a single global max_snapshot_date, which silently
    # excluded any account whose latest snapshot was older than the global max.
    from app.engine.holdings_view import latest_holdings as _latest_holdings
    holdings_rows = _latest_holdings(db, owner_ids=all_owner_ids)

    tagged_corpus: dict[str, float] = {}
    untagged_equity = 0.0
    untagged_mf     = 0.0
    for h in holdings_rows:
        val = float(h.current_value or 0)
        if h.goal_tag:
            tagged_corpus[h.goal_tag] = tagged_corpus.get(h.goal_tag, 0.0) + val
        else:
            ht = (h.holding_type or "").lower()
            if ht == "equity":
                untagged_equity += val
            elif ht == "mutual_fund":
                untagged_mf += val

    untagged_pool = untagged_equity + untagged_mf

    # ── Goals eligible for auto-refresh ───────────────────────────────────────
    # Goals are stored under "family" owner_id in config, so always include it
    goals = db.query(Goal).filter(Goal.owner_id.in_(all_owner_ids + ["family"])).all()
    invest_goals = [g for g in goals
                    if g.goal_type in ("retirement", "child_education", "child_marriage")]

    # Proportional split of untagged pool among investment goals (by target_amount)
    total_target = sum(float(g.target_amount or 0) for g in invest_goals)

    def _untagged_share(goal: Goal) -> float:
        if not invest_goals or untagged_pool == 0:
            return 0.0
        if total_target > 0:
            share = float(goal.target_amount or 0) / total_target
        else:
            share = 1.0 / len(invest_goals)
        return round(untagged_pool * share, 2)

    def _candidate_tags(goal: Goal) -> list[str]:
        """
        Map a goal to the goal_tag(s) that should contribute to its corpus.
        Distinguishes Your Retirement (owner=user → tag "retirement") from
        Wife's Retirement (owner=wife → tag "wife_retirement"); without this
        both retirement goals previously pulled the same tagged_corpus key
        and the dedicated wife_retirement tag was orphaned.
        """
        gt = goal.goal_type
        owner = (goal.owner_id or "").lower()
        if gt == "retirement":
            # Heuristic: any non-primary-user owner with a retirement goal
            # gets the wife_retirement tag pool.
            if owner in ("wife", "spouse", "partner") or "wife" in (goal.name or "").lower():
                return ["wife_retirement"]
            return ["retirement"]
        return [gt]

    updated = 0
    for g in goals:
        gt = g.goal_type
        if gt == "custom":
            continue  # user manages manually

        tags = _candidate_tags(g)
        tag_sum = sum(tagged_corpus.get(t, 0.0) for t in tags)

        if tag_sum > 0:
            base = tag_sum
            if gt == "retirement":
                # Per-owner PPF — your retirement gets your PPF, wife's gets
                # hers. If goal is family-owned (uncommon), use family total.
                base += _ppf_for(g.owner_id or "family")
            new_corpus = Decimal(str(round(base, 2)))
        elif gt in ("retirement", "child_education", "child_marriage"):
            base = _untagged_share(g)
            if gt == "retirement":
                base += _ppf_for(g.owner_id or "family")
            new_corpus = Decimal(str(round(base, 2)))
        elif gt in ("emergency_fund", "emergency"):
            new_corpus = Decimal(str(round(bank + liquid_extra, 2)))
        elif gt == "health":
            new_corpus = Decimal(str(round(fd + other, 2)))
        else:
            continue

        if g.current_corpus != new_corpus:
            g.current_corpus = new_corpus
            updated += 1

    if updated:
        db.commit()
    return updated


def _recent_investing_rate(
    db: Session, owner_ids: list[str], lookback_months: int = 6,
    today: date | None = None,
) -> float:
    """
    Actual investing rate (₹/month) over the last `lookback_months`.

    Sums:
      - Debit transactions on investment accounts (stocks / mutual_fund / demat),
        skipping the Zerodha monthly-demat unit-count rows (description begins
        with "DEMAT ").
      - ManualAssetContribution rows (PPF / NPS / RD installments) in the
        same window.

    Used as the *real* current_monthly_sip in goal projections so on_track and
    gap reflect what the user is actually doing, not the SIP that would make
    every goal on-track by construction.
    """
    from datetime import timedelta
    from sqlalchemy import func as _f
    from app.models.schemas import (
        Account, Transaction, ManualAsset, ManualAssetContribution,
    )

    today  = today or date.today()
    cutoff = today - timedelta(days=int(30.44 * lookback_months))

    inv_debits = (
        db.query(_f.coalesce(_f.sum(Transaction.amount), 0))
        .join(Account, Transaction.account_id == Account.id)
        .filter(
            Account.owner_id.in_(owner_ids),
            Account.account_type.in_(("stocks", "mutual_fund", "demat")),
            Transaction.transaction_type == "debit",
            Transaction.date >= cutoff,
            ~Transaction.description.ilike("DEMAT %"),
        )
        .scalar()
    ) or 0

    contribs = (
        db.query(_f.coalesce(_f.sum(ManualAssetContribution.amount), 0))
        .join(ManualAsset, ManualAsset.id == ManualAssetContribution.asset_id)
        .filter(
            ManualAsset.owner_id.in_(owner_ids),
            ManualAssetContribution.date >= cutoff,
        )
        .scalar()
    ) or 0

    months = max(1, lookback_months)
    return float(inv_debits + contribs) / months


def project_all_goals(db: Session, owner_id: str | None = None) -> list[GoalProjection]:
    from app.models.schemas import Owner

    q = db.query(Goal)
    # "family" should show every household member's goals (Your + Wife's
    # Retirement are owned by user/wife, not literal "family"), so we expand
    # rather than filter on the literal owner_id.
    if owner_id and owner_id != "family":
        q = q.filter(Goal.owner_id.in_([owner_id, "family"]))
    goals = q.all()

    # Resolve owner_ids for the actual-investing calculation. "family" expands
    # to every real owner so the family's combined SIPs feed every goal.
    if owner_id == "family" or owner_id is None:
        owner_ids = [r[0] for r in db.query(Owner.id).filter(Owner.id != "family").all()]
    else:
        owner_ids = [owner_id]

    actual_monthly = _recent_investing_rate(db, owner_ids, lookback_months=6)

    # Distribute the family's actual investing across investment goals in
    # proportion to each goal's prescribed monthly_sip_needed. This answers
    # "given how I'm actually splitting my SIPs, where am I headed?" rather
    # than the trivially-circular "if I pay the prescribed SIP, am I on track?".
    investment_goals = [g for g in goals if (g.goal_type or "") not in ("emergency", "custom")]
    total_needed = sum(float(g.monthly_sip_needed or 0) for g in investment_goals)

    def _actual_for(goal: Goal) -> float:
        if total_needed <= 0 or actual_monthly <= 0:
            return 0.0
        if (goal.goal_type or "") in ("emergency", "custom"):
            return 0.0  # emergency fund isn't where SIPs go
        share = float(goal.monthly_sip_needed or 0) / total_needed
        return actual_monthly * share

    projections = []
    for goal in goals:
        projections.append(project_goal(goal, _actual_for(goal), db))

    return projections


def update_goal_sip(db: Session, goal_id: str) -> None:
    """Recalculate and persist monthly_sip_needed for a goal."""
    goal = db.query(Goal).filter_by(id=goal_id).first()
    if not goal:
        return
    proj = project_goal(goal, 0, db)
    goal.monthly_sip_needed = Decimal(str(proj.monthly_sip_needed))
    db.commit()
