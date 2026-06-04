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
    # ── New: projection inputs surfaced for UI ─────────────────────────────
    effective_return_pct:   Optional[float] = None   # final return used (after blend + cap)
    return_source:          str = "manual"           # "manual" | "blended" | "default"
    sip_stepup_pct:         Optional[float] = None   # step-up applied to current SIP
    sip_stepup_source:      str = "none"             # "manual" | "auto" | "none"
    glide_active:           bool = False             # glide path applied
    blended_components:     Optional[list] = None    # for tooltip in UI


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

    # ── Pick effective return (manual override vs blended from tagged holdings) ─
    use_manual = bool(getattr(goal, "use_manual_return", False))
    blended_info = None
    return_source = "manual"
    effective_return = manual_ret

    if not use_manual and db is not None and owner_ids:
        try:
            from app.engine.goal_returns import (
                compute_blended_return, DEFAULT_RETURN_PCT,
            )
            blended_info = compute_blended_return(db, goal, owner_ids, today)
            if blended_info.get("blended_pct") is not None:
                effective_return = blended_info["blended_pct"]
                return_source = "blended"
            else:
                effective_return = DEFAULT_RETURN_PCT
                return_source = "default"
        except Exception:
            effective_return = manual_ret
            return_source = "manual"

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
    stored_inf = float(goal.inflation_rate or 0)
    inf_rate   = get_india_cpi() if stored_inf == 6.0 else stored_inf
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
        blended_components=(blended_info or {}).get("components") if blended_info else None,
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

    # Sum broker-account debits, counting only groups that look like real SIPs:
    #   1. At least `min_recurrence` hits in the window (regular cadence), AND
    #   2. Amounts are tight — coefficient of variation < 15% (fixed SIP amount,
    #      not discretionary buys of varying size like equity / ETF purchases).
    rows = (
        db.query(
            Transaction.account_id,
            _f.coalesce(Transaction.counterparty, Transaction.description).label("flow_key"),
            _f.count(Transaction.id).label("n"),
            _f.sum(Transaction.amount).label("total"),
            _f.avg(Transaction.amount).label("avg_amt"),
            _f.stddev_samp(Transaction.amount).label("sd_amt"),
        )
        .join(Account, Transaction.account_id == Account.id)
        .filter(
            Account.owner_id.in_(owner_ids),
            Account.account_type.in_(("stocks", "mutual_fund", "demat")),
            Transaction.transaction_type == "debit",
            Transaction.date >= cutoff,
            ~Transaction.description.ilike("DEMAT %"),
        )
        .group_by(Transaction.account_id,
                  _f.coalesce(Transaction.counterparty, Transaction.description))
        .all()
    )
    # For each qualifying SIP flow, the monthly rate is the per-txn average
    # (one fixed-amount SIP per month). Summing avg_amt across flows gives the
    # *current* monthly SIP rate — not the trailing 6-month average, which
    # under-reports SIPs that started recently.
    min_recurrence = max(3, lookback_months // 2)
    sip_monthly = 0.0
    for r in rows:
        if (r.n or 0) < min_recurrence:
            continue
        avg = float(r.avg_amt or 0)
        sd  = float(r.sd_amt or 0)
        if avg <= 0:
            continue
        cv = sd / avg
        if cv > 0.15:           # amounts vary too much → discretionary, not SIP
            continue
        # SIPs are set in round-rupee amounts; Coin/AMC debit a paisa or two
        # less due to NAV rounding (e.g. ₹19,999 instead of ₹20,000). Snap to
        # the nearest ₹100 so the reported total matches the configured SIP.
        sip_monthly += round(avg / 100.0) * 100

    # Bank-account ACH outflows to broker mandates (Zerodha/Coin SIPs that
    # ride on the bank statement, not the broker statement). Group by mandate
    # amount and require the same SIP-cadence + tight-amount rules so we get
    # the current monthly rate, not a windowed average.
    ach_rows = (
        db.query(
            Transaction.amount,
            _f.count(Transaction.id).label("n"),
            _f.avg(Transaction.amount).label("avg_amt"),
            _f.stddev_samp(Transaction.amount).label("sd_amt"),
        )
        .join(Account, Transaction.account_id == Account.id)
        .filter(
            Account.owner_id.in_(owner_ids),
            Account.account_type == "bank",
            Transaction.transaction_type == "debit",
            Transaction.date >= cutoff,
            (
                Transaction.description.ilike("%YESB00709000028661%")
                | Transaction.description.ilike("%ZERODHA BROKIN%")
                | Transaction.description.ilike("%INDIAN CLEARIN%")
            ),
        )
        .group_by(Transaction.amount)
        .all()
    )
    broker_ach_monthly = 0.0
    for r in ach_rows:
        if (r.n or 0) < min_recurrence:
            continue
        avg = float(r.avg_amt or 0)
        sd  = float(r.sd_amt or 0)
        if avg <= 0:
            continue
        if (sd / avg) > 0.15:
            continue
        broker_ach_monthly += avg

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
    # sip_monthly + broker_ach_monthly are already monthly rates (per-txn avg
    # of the detected recurring flow). Only manual contributions still need
    # to be divided over the lookback window.
    return sip_monthly + broker_ach_monthly + (float(contribs) / months)


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

    from app.engine.goal_returns import (
        actual_monthly_sip_for_goal, detect_sip_stepup,
    )

    detected_stepup = detect_sip_stepup(db, owner_ids)
    actual_family_total = _recent_investing_rate(db, owner_ids, lookback_months=6)

    investment_goals = [g for g in goals if (g.goal_type or "") not in ("emergency", "custom")]

    # ── Per-goal actual SIP from tagged holdings (preferred) ────────────────
    per_goal_actual: dict[str, float] = {}
    sum_tagged_actual = 0.0
    for g in investment_goals:
        v = actual_monthly_sip_for_goal(db, g, owner_ids, lookback_months=6)
        per_goal_actual[str(g.id)] = v
        sum_tagged_actual += v

    # ── Fallback: if tagging is sparse, spread the *un-attributed* family SIP
    # in proportion to monthly_sip_needed so no contribution is silently lost.
    untagged_family = max(0.0, actual_family_total - sum_tagged_actual)
    total_needed = sum(float(g.monthly_sip_needed or 0) for g in investment_goals)

    def _actual_for(goal: Goal) -> float:
        if (goal.goal_type or "") in ("emergency", "custom"):
            return 0.0
        base = per_goal_actual.get(str(goal.id), 0.0)
        if untagged_family > 0 and total_needed > 0:
            share = float(goal.monthly_sip_needed or 0) / total_needed
            base += untagged_family * share
        return base

    projections = []
    for goal in goals:
        projections.append(project_goal(
            goal, _actual_for(goal), db,
            owner_ids=owner_ids,
            detected_stepup_pct=detected_stepup,
        ))

    return projections


def update_goal_sip(db: Session, goal_id: str) -> None:
    """Recalculate and persist monthly_sip_needed for a goal."""
    goal = db.query(Goal).filter_by(id=goal_id).first()
    if not goal:
        return
    proj = project_goal(goal, 0, db)
    goal.monthly_sip_needed = Decimal(str(proj.monthly_sip_needed))
    db.commit()
