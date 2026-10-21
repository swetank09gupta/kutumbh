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


@dataclass
class GoalProjection:
    goal_id:            str
    name:               str
    goal_type:          str
    target_date:        Optional[date]
    inflation_adj_target: float   # target in today's ₹ terms
    current_corpus:     float
    monthly_sip_needed: float     # to reach target from today
    projected_corpus:   float     # if current SIP maintained
    gap:                float     # projected_corpus - inflation_adj_target
    months_to_goal:     int
    on_track:           bool
    current_monthly_sip: float    # what they're actually investing (estimated)
    # ── New: projection inputs surfaced for UI ─────────────────────────────
    effective_return_pct:   Optional[float] = None   # final return used (after blend + cap)
    return_source:          str = "typed"            # "market" (the planner) | "typed" (the return on the goal)
    sip_stepup_pct:         Optional[float] = None   # step-up applied to current SIP
    sip_stepup_source:      str = "none"             # "manual" | "auto" | "none"
    glide_active:           bool = False             # glide path applied
    # ── From the planner (app/planning): chance, derived target, inflation used. None while market history is unavailable.
    chance:                 Optional[float] = None   # share of simulated futures in which the plan covers the goal
    sip_for_90:             Optional[float] = None
    ideal_target_today:     Optional[float] = None   # derived from spending, inflation and the date (today's money)
    typed_target_today:     Optional[float] = None   # the figure typed in the app
    target_source:          Optional[str] = None     # "derived" | "typed"
    inflation_used_pct:     Optional[float] = None
    plan_note:              Optional[str] = None
    funding_rank:           Optional[int] = None     # the order money should reach the goals (emergency fund, then by date)
    suggested_sip:          Optional[float] = None   # what that order gives this goal out of the household's monthly capacity
    chance_at_suggested:    Optional[float] = None   # the chance of meeting it at that saving


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

    if gt == "retirement" and (goal.owner_id in (None, "", "family")):
        dates = [_add_years(o.birth_date, int(o.retirement_age)) for o in db.query(Owner).filter(Owner.id != "family").all()
                 if o.birth_date and o.retirement_age]
        return min(dates) if dates else stored                       # one household retirement: it starts when the first earner stops

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
    Kept for callers that don't need step-up / glide path.
    """
    r = annual_rate / 12 / 100
    if r == 0:
        remaining = future_value - pv
        return max(0, remaining / months)

    fv_of_pv  = _future_value_lump_sum(pv, annual_rate, months)
    remaining = future_value - fv_of_pv
    if remaining <= 0:
        return 0.0

    denominator = ((1 + r) ** months) - 1
    return (remaining * r) / denominator if denominator > 0 else 0.0


# ── Piecewise projection (supports glide path + SIP step-up) ────────────────
def _monthly_return_schedule(
    start_return_pct: float, months: int,
    glide_enabled: bool, glide_start_years_before: int, glide_end_return_pct: float,
) -> list[float]:
    """
    Per-month return rate (decimal, monthly compounded) for `months` ahead.
    If glide is enabled and the horizon is long enough, the rate linearly tapers
    from `start_return_pct` to `glide_end_return_pct` over the last
    `glide_start_years_before` years.
    """
    glide_months = (glide_start_years_before or 0) * 12
    if not glide_enabled or glide_months <= 0 or months <= glide_months:
        r = start_return_pct / 12 / 100
        return [r] * months

    flat_months = months - glide_months
    flat_r = start_return_pct / 12 / 100
    schedule = [flat_r] * flat_months
    # Linear taper over glide_months from start_return_pct to glide_end_return_pct
    for i in range(glide_months):
        frac = (i + 1) / glide_months
        annual = start_return_pct + (glide_end_return_pct - start_return_pct) * frac
        schedule.append(annual / 12 / 100)
    return schedule


def _fv_lump_with_schedule(pv: float, schedule: list[float]) -> float:
    """Compound a lump sum month-by-month under varying monthly rates."""
    v = pv
    for r in schedule:
        v *= (1.0 + r)
    return v


def _fv_sip_with_stepup(
    initial_monthly_sip: float, stepup_pct: float, schedule: list[float],
) -> float:
    """
    Future value of a stepped-up SIP under a per-month return schedule.
    Step-up applied each completed 12 months: SIP_year_n = initial * (1+s)^n.
    """
    if initial_monthly_sip <= 0 or not schedule:
        return 0.0
    s = (stepup_pct or 0.0) / 100.0
    v = 0.0
    sip = initial_monthly_sip
    for idx, r in enumerate(schedule):
        # bump SIP at the start of each new year (months 12, 24, ...)
        if idx > 0 and idx % 12 == 0:
            sip *= (1.0 + s)
        v = (v + sip) * (1.0 + r)
    return v


def _solve_sip_with_stepup(
    fv_target: float, pv: float, schedule: list[float], stepup_pct: float,
) -> float:
    """
    Numerically solve for the initial monthly SIP that, with step-up applied
    each year and growth following `schedule`, reaches `fv_target` together with
    the projected growth of `pv`.
    """
    fv_pv = _fv_lump_with_schedule(pv, schedule)
    remaining = fv_target - fv_pv
    if remaining <= 0:
        return 0.0
    # Binary search — monotone in initial SIP.
    lo, hi = 0.0, max(remaining, 1.0)
    # Expand `hi` until it overshoots.
    for _ in range(40):
        if _fv_sip_with_stepup(hi, stepup_pct, schedule) >= remaining:
            break
        hi *= 2
    for _ in range(60):
        mid = (lo + hi) / 2
        if _fv_sip_with_stepup(mid, stepup_pct, schedule) < remaining:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _inflation_adjusted_target(nominal_target: float, inflation_rate: float,
                                years: float) -> float:
    """Target in today's money terms (already inflation-adjusted by user usually)."""
    # If the user set target in today's money, we grow it by inflation to get future value
    return nominal_target * ((1 + inflation_rate / 100) ** years)


def project_goal(goal: Goal, current_monthly_sip: float = 0,
                 db: Session | None = None,
                 owner_ids: list[str] | None = None,
                 detected_stepup_pct: Optional[float] = None) -> GoalProjection:
    today      = date.today()
    effective_target = _age_target_date(goal, db) if db is not None else goal.target_date
    if effective_target is None:
        effective_target = goal.target_date
    months     = _months_between(today, effective_target)
    years      = months / 12

    pv         = float(goal.current_corpus or 0)
    target_nom = float(goal.target_amount or 0)
    manual_ret = float(goal.expected_return_rate or 12)

    # ── Emergency fund: a floor to maintain, not a dated target ──────────────
    # No target date, no inflation compounding, no market return assumed. The question is only
    # "is the liquid corpus at least the target, and if not, what monthly amount closes the gap
    # within a year".
    if (goal.goal_type or "").lower() in ("emergency", "emergency_fund"):
        shortfall = max(0.0, target_nom - pv)
        return GoalProjection(
            goal_id=str(goal.id), name=goal.name, goal_type=goal.goal_type or "emergency",
            target_date=None, inflation_adj_target=round(target_nom, 2), current_corpus=pv,
            monthly_sip_needed=round(shortfall / 12, 2), projected_corpus=round(pv, 2),
            gap=round(pv - target_nom, 2), months_to_goal=0, on_track=shortfall <= 0,
            current_monthly_sip=current_monthly_sip, effective_return_pct=None,          # cash earns what it earns: no return is projected
            return_source="typed", sip_stepup_pct=0.0, sip_stepup_source="none", glide_active=False,
        )

    # ── Return: only goals the market plan does not cover reach here (custom goals, or no market data), so it is the return typed on the goal ──
    return_source = "typed"
    effective_return = manual_ret

    # ── Step-up (manual value wins; else auto-detected; else 0) ──────────────
    stored_stepup = getattr(goal, "sip_stepup_pct", None)
    stepup_source = "none"
    if stored_stepup is not None:
        stepup_pct = float(stored_stepup)
        stepup_source = "manual"
    elif detected_stepup_pct is not None:
        stepup_pct = detected_stepup_pct
        stepup_source = "auto"
    else:
        stepup_pct = 0.0

    # ── Glide path ──────────────────────────────────────────────────────────
    glide_enabled  = bool(getattr(goal, "glide_enabled", True))
    glide_yrs      = int(getattr(goal, "glide_start_years_before", 3) or 0)
    glide_end_pct  = float(getattr(goal, "glide_end_return_pct", 8.0) or 8.0)
    schedule = _monthly_return_schedule(
        effective_return, months, glide_enabled, glide_yrs, glide_end_pct,
    )
    glide_active = glide_enabled and months > glide_yrs * 12 and glide_yrs > 0

    # ── Inflation-adjusted target ───────────────────────────────────────────
    # One inflation assumption for the whole app (app_settings), never a per-goal number and never
    # the latest one-year CPI print (2025's 2.4% would shrink a 20-year target and the SIP for it).
    from app.engine.settings_store import DEFAULT_INFLATION, get_inflation
    inf_rate   = get_inflation(db) if db is not None else DEFAULT_INFLATION
    inf_adj_target = _inflation_adjusted_target(target_nom, inf_rate, years)

    # ── SIP needed (solves under step-up + schedule) ────────────────────────
    sip_needed = _solve_sip_with_stepup(inf_adj_target, pv, schedule, stepup_pct)

    # ── Projected corpus if user keeps their current SIP (with step-up) ─────
    fv_pv  = _fv_lump_with_schedule(pv, schedule)
    fv_sip = _fv_sip_with_stepup(current_monthly_sip, stepup_pct, schedule)
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
        effective_return_pct=round(effective_return, 2),
        return_source=return_source,
        sip_stepup_pct=round(stepup_pct, 2) if stepup_pct else 0.0,
        sip_stepup_source=stepup_source,
        glide_active=glide_active,
    )


def refresh_corpus_from_networth(db: Session, owner_id: str) -> int:
    """
    Auto-set current_corpus for each goal from the owner's latest holdings + net worth.

    Priority for equity/MF holdings:
      1. If a holding has goal_tag set, its value goes exclusively to that goal.
      2. Untagged holdings are split proportionally by target_amount across goals of
         the same asset class (retirement vs child goals), preventing double-counting.

    The emergency fund is the cash you can reach: bank balances + every live FD / RD (net-worth snapshot). Everything else (stocks, funds, bonds, gold,
    PPF, EPF) is matched to the other goals (app/planning/matching.py).

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
    fd    = float(nw.fd_value     or 0)            # live FDs and RDs

    # When owner_id is "family" we want all individual owners, not just rows
    # literally tagged "family" (which don't exist on the accounts table).
    # Expand "family" → every owner id that exists.
    if owner_id == "family":
        all_owner_ids = [r[0] for r in db.query(Owner.id).all()]
    else:
        all_owner_ids = [owner_id]

    # ── Tagged holdings: sourced from the deduped latest-holdings VIEW ────────
    # The view already handles per-account latest snapshot and dedups the same
    # ISIN appearing in multiple accounts (CDSL + Zerodha mirror each other).
    # Earlier this used a single global max_snapshot_date, which silently
    # excluded any account whose latest snapshot was older than the global max.
    from app.engine.holdings_view import latest_holdings as _latest_holdings
    holdings_rows = _latest_holdings(db, owner_ids=all_owner_ids)

    untagged_equity = 0.0
    untagged_mf     = 0.0
    for h in holdings_rows:
        val = float(h.current_value or 0)
        if not h.goal_tag:
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

    from app.engine.goal_returns import goal_tags as _candidate_tags

    from app.planning import goal_plan

    updated = 0
    for g in goals:
        gt = g.goal_type
        if gt == "custom":
            continue  # user manages manually
        if gt in ("emergency_fund", "emergency"):
            new_corpus = Decimal(str(round(bank + fd, 2)))          # cash in the bank and every live FD / RD, nothing else
        elif gt in ("retirement", "child_education", "child_marriage", "health"):
            # everything tagged to the goal, holdings and manual assets alike (PPF, EPF, FDs, SGBs): the planner's own list, one definition
            base = sum(v for _o, v, _w in goal_plan.holdings_by_owner(db, _candidate_tags(g), _scope(g, all_owner_ids)))
            if base == 0 and gt != "health":
                base = _untagged_share(g)
            new_corpus = Decimal(str(round(base, 2)))
        else:
            continue

        if g.current_corpus != new_corpus:
            g.current_corpus = new_corpus
            updated += 1

    if updated:
        db.commit()
    return updated


def _scope(goal: Goal, all_owner_ids: list[str]) -> list[str]:
    """Whose money can fund a goal: the owner's own for a personal goal, everyone's for a family goal."""
    return list(all_owner_ids) if (goal.owner_id in (None, "", "family")) else [goal.owner_id]


def project_all_goals(db: Session, owner_id: str | None = None, scenario: str = "base", stepup: float | None = None) -> list[GoalProjection]:
    """
    One projection per goal — the only calculator (the goal cards, the projections table, the
    stored SIP column and Grafana all read it).

    Current monthly SIP comes from the exact trade ledger (app.engine.sip): funds bought on a
    regular cadence. A SIP tagged to a goal counts toward that goal only (and only the owner's
    own SIPs for a personal goal). SIPs with no goal tag are split across the goals that owner can
    fund, weighted by target amount. Nobody's SIP is counted twice or credited to someone else.
    """
    from collections import defaultdict
    from app.engine import sip as sipmod
    from app.engine.goal_returns import goal_tags
    from app.engine.holdings_view import latest_holdings
    from app.engine.networth import real_owners
    from app.models.schemas import ManualAsset, ManualAssetContribution
    from sqlalchemy import func as _f
    from datetime import timedelta

    all_owner_ids = [o.id for o in real_owners(db)]
    q = db.query(Goal)
    if owner_id and owner_id != "family":
        q = q.filter(Goal.owner_id.in_([owner_id, "family"]))
    goals = q.all()
    investing = [g for g in goals if (g.goal_type or "").lower() not in ("emergency", "emergency_fund", "custom")]

    sips = sipmod.monthly_sip_by_fund(db, all_owner_ids, 6)
    tag_of = {(h.owner_id, h.isin): h.goal_tag
              for h in latest_holdings(db, owner_ids=all_owner_ids) if h.isin}
    actual: dict[str, float] = defaultdict(float)
    by_owner: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))     # goal id -> owner -> monthly SIP
    untagged: dict[str, float] = defaultdict(float)

    def place(owner: str, tag: Optional[str], amount: float) -> None:
        if tag:
            for g in investing:
                if tag in goal_tags(g) and owner in _scope(g, all_owner_ids):
                    actual[str(g.id)] += amount
                    by_owner[str(g.id)][owner] += amount
                    return
        untagged[owner] += amount

    for (o, isin), amt in sips.items():
        place(o, tag_of.get((o, isin)), amt)

    # PPF / NPS / RD instalments recorded against manual assets (monthly average of the last 6 months)
    cutoff = date.today() - timedelta(days=int(30.44 * 6))
    for owner, tag, total in (
        db.query(ManualAsset.owner_id, ManualAsset.goal_tag, _f.sum(ManualAssetContribution.amount))
        .join(ManualAsset, ManualAsset.id == ManualAssetContribution.asset_id)
        .filter(ManualAsset.owner_id.in_(all_owner_ids), ManualAssetContribution.date >= cutoff)
        .group_by(ManualAsset.owner_id, ManualAsset.goal_tag).all()
    ):
        place(owner, tag, float(total or 0) / 6)

    for owner, amount in untagged.items():
        eligible = [g for g in investing if owner in _scope(g, all_owner_ids)]
        if not eligible:
            continue
        total_t = sum(float(g.target_amount or 0) for g in eligible)
        for g in eligible:
            share = (float(g.target_amount or 0) / total_t) if total_t else 1 / len(eligible)
            actual[str(g.id)] += amount * share
            by_owner[str(g.id)][owner] += amount * share

    out = []
    funded: list = []
    ctx = None
    try:
        from app.planning import goal_plan
        ctx = goal_plan.get_context(db, scenario, goal_plan.DEFAULT_STEPUP if stepup is None else stepup)
    except Exception:                                   # planning must never take the goal page down
        import logging
        logging.getLogger(__name__).exception("planning unavailable")
    for g in goals:
        scope = _scope(g, all_owner_ids)
        proj = project_goal(
            g, round(actual.get(str(g.id), 0.0), 2), db, owner_ids=scope,
            detected_stepup_pct=sipmod.sip_stepup_pct(db, scope))
        planned = False
        if ctx is not None:
            try:
                planned = goal_plan.overlay(db, g, proj, dict(by_owner.get(str(g.id), {})), scope, ctx)
                if planned:
                    funded.append((g, proj, dict(by_owner.get(str(g.id), {})), scope))
            except Exception:
                import logging
                logging.getLogger(__name__).exception("plan for %s failed; keeping the deterministic projection", g.name)
        if not planned and (g.goal_type or "").lower() not in ("custom", ""):
            proj.plan_note = "Simple projection on your typed return (the market plan is not available for this goal right now)."
        out.append(proj)
    if ctx is not None and funded:
        try:
            goal_plan.apply_funding(db, ctx, funded)            # the order and size of what each goal should get from the monthly capacity
        except Exception:
            import logging
            logging.getLogger(__name__).exception("funding order failed")
    return out


def refresh_goals(db: Session) -> dict:
    """
    Bring every goal in line with today's holdings: corpus from the tagged holdings, then the
    stored monthly_sip_needed from the single projection. Run after the ledger refresh and the
    net-worth snapshot so corpus, SIP and the cards always describe the same portfolio.
    """
    try:
        from app.engine import epf as _epf
        _epf.sync(db)                                      # a balance shared in config/accounts.yml becomes (or updates) the EPF asset
    except Exception:
        import logging
        logging.getLogger(__name__).exception("EPF sync failed")
    corpus = refresh_corpus_from_networth(db, "family")
    stored = 0
    for p in project_all_goals(db):
        g = db.get(Goal, p.goal_id)
        new = Decimal(str(p.monthly_sip_needed))
        if g is not None and g.monthly_sip_needed != new:
            g.monthly_sip_needed = new
            stored += 1
    db.commit()
    return {"corpus_updated": corpus, "sip_updated": stored}


def update_goal_sip(db: Session, goal_id: str) -> None:
    """Recalculate and persist monthly_sip_needed (from the single projection, not a private formula)."""
    for p in project_all_goals(db):
        if p.goal_id == str(goal_id):
            goal = db.query(Goal).filter_by(id=goal_id).first()
            if goal:
                goal.monthly_sip_needed = Decimal(str(p.monthly_sip_needed))
                db.commit()
            return
