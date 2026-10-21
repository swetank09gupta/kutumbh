"""
Turns each goal into contributions and outflows and runs the one planning engine (app/planning/simulate.py). Called by the goal
calculator (`app.engine.goals.project_goal`), so the goal cards, the projections table, the stored SIP and Grafana all show these numbers.
docs/PLANNING.md. Nothing is typed here: spending, ages, regimes, tags and SIPs come from the app; inflation, returns and tax are derived.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Optional

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.planning import allocation, expectations, history, longevity, macro, need, simulate, taxes
from app.planning.assumptions import Assumptions, with_drags
from app.planning.macro import param

logger = logging.getLogger(__name__)

CONFIDENCE = 0.8                       # the "ideal target" and the SIP shown are the ones that give this chance
RETIRE_MIX = {"equity": 0.40, "gold": 0.05, "debt": 0.55}
DEFAULT_STEPUP = 0.08
_TTL = 600
_CTX: dict = {}
_RESULTS: dict = {}


@dataclass
class Context:
    today: date
    macro: dict
    data: dict
    base: Assumptions
    why: dict
    profiles: dict
    spending: dict
    owners: dict
    salary: dict
    loan: Optional[dict]
    stepup: float = DEFAULT_STEPUP


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def _months(today: date, d: date) -> int:
    return max(1, (d.year - today.year) * 12 + (d.month - today.month))


def build_context(db: Session, today: date | None = None, scenario: str = "base", stepup: float = DEFAULT_STEPUP,
                  macro_snapshot: dict | None = None, data: dict | None = None, profiles: dict | None = None) -> Context:
    from app.engine.networth import real_owners
    from app.engine.small_savings import ppf_rate_on
    today = today or date.today()
    data = data or history.load(today=today)
    snap = macro_snapshot or macro.snapshot(today)
    try:
        ppf = float(ppf_rate_on(today)) / 100
    except Exception:
        ppf = None
    base, why = expectations.derive(snap, data, scenario, ppf=ppf)
    owners = {o.id: {"name": o.name, "dob": o.birth_date, "retirement_age": int(o.retirement_age) if o.retirement_age else None,
                     "risk": (o.risk_appetite or "moderate").lower(), "regime": (o.tax_regime or "old").lower()} for o in real_owners(db)}
    sal = {oid: float(db.execute(text("SELECT COALESCE(SUM(amount), 0) FROM v_cashflow WHERE flow = 'income' AND subcategory = 'salary' "
                                      "AND owner_id = :o AND month >= :s AND month < :e"), {"o": oid, "s": need.month_window(today)[0],
                                                                                              "e": need.month_window(today)[1]}).scalar() or 0)
           for oid in owners}
    loan = db.execute(text("SELECT principal_after, emi, rate, emis_left FROM loan_anchors ORDER BY as_on DESC LIMIT 1")).first()
    spending = need.household_spending(db, today)
    spending["trend"] = need.spending_trend(db, today)
    g = spending["trend"]["growth_pct"]
    cap = float(param("spending_trend", "drift_cap_pp")["value"])
    base = replace(base, spending_drift=need.drift_from_growth(g, base.inflation_general * 100, cap))
    return Context(today=today, macro=snap, data=data, base=base, why=why,
                   profiles=profiles if profiles is not None else taxes.person_profiles(db, today),
                   spending=spending, owners=owners, salary=sal,
                   loan={"outstanding": float(loan.principal_after), "emi": float(loan.emi), "rate_pct": float(loan.rate)} if loan else None,
                   stepup=stepup)


def get_context(db: Session, scenario: str = "base", stepup: float = DEFAULT_STEPUP, today: date | None = None) -> Optional[Context]:
    """Cached for ten minutes; None when market history is unavailable (the calculator then keeps its deterministic projection)."""
    key = (scenario, round(stepup, 4), today or date.today())
    hit = _CTX.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    try:
        ctx = build_context(db, today, scenario, stepup)
    except history.PlanningDataUnavailable as exc:
        logger.warning("planning unavailable: %s", exc)
        return None
    except Exception as exc:                                    # a planning failure must never break the goal page
        logger.exception("planning context failed: %s", exc)
        return None
    _CTX[key] = (time.time(), ctx)
    return ctx


def holdings_by_owner(db: Session, tags: list[str], scope: list[str]) -> list[tuple[str, float, dict]]:
    """(owner, value, asset weights) for everything tagged to a goal."""
    from app.engine.holdings_view import latest_holdings
    from app.models.schemas import ManualAsset
    out = []
    for h in latest_holdings(db, owner_ids=scope):
        if h.goal_tag in tags and float(h.current_value or 0) > 0:
            out.append((h.owner_id, float(h.current_value), allocation.classify(h.instrument_name, h.holding_type)))
    for m in db.query(ManualAsset).filter(ManualAsset.owner_id.in_(scope), ManualAsset.goal_tag.in_(tags), ManualAsset.is_liquid == True).all():  # noqa: E712  (a home you live in is not a corpus: the goal's own corpus leaves it out too)
        if float(m.current_value or 0) > 0:
            out.append((m.owner_id, float(m.current_value), allocation.classify(m.name, None, m.asset_type)))
    return out


def _goal_assumptions(ctx: Context, items: list, years: float, retire_income_today: float | None = None) -> tuple[Assumptions, dict]:
    """Assumptions with the tax drags of this goal's holders; the mix and owner weights come from what is tagged to it."""
    by_owner: dict[str, float] = {}
    for o, v, _w in items:
        by_owner[o] = by_owner.get(o, 0.0) + v
    debt_rate = taxes.weighted_debt_rate(ctx.profiles, by_owner)
    fy = next(iter(ctx.profiles.values()))["fy"] if ctx.profiles else "2026-27"
    income = retire_income_today if retire_income_today is not None else ctx.spending["retirement_core"] / 2
    saving, retired = taxes.drags(ctx.base, years, debt_rate, income, ctx.today, fy)
    return with_drags(ctx.base, saving, retired), {"debt_rate_pct": round(debt_rate * 100, 1), "owners": by_owner}


def _risk_shift(ctx: Context, owner_values: dict[str, float]) -> str:
    tot = sum(owner_values.values()) or 1.0
    s = sum(owner_values.get(o, 0) * {"conservative": -1, "aggressive": 1}.get(ctx.owners.get(o, {}).get("risk", "moderate"), 0) for o in owner_values) / tot
    return "conservative" if s < -0.4 else "aggressive" if s > 0.4 else "moderate"


def _result(ctx, a, res, shape, scale_now, corpus0, first_out: int, outflow_nom_p50: float, ideal_today: float, basis: str, infl_used: float,
            years: float, weights, extra: dict | None = None) -> dict:
    """Common shape of a goal's plan result. 'required' is the corpus needed when spending starts, at CONFIDENCE; 'projected' the median."""
    D = res["D"]
    O_at_start = None
    need_ = res["need"]
    B_start = np.cumsum(shape[None, :] * D, axis=1)[:, first_out]
    corpus_start = (corpus0 + scale_now * B_start) / D[:, first_out]
    out = {"chance": res["probability"], "sip_for_80": res["scale_for_confidence"].get("80"), "sip_for_90": res["scale_for_confidence"].get("90"),
           "sip_for_50": res["scale_for_confidence"].get("50"), "ideal_target_today": ideal_today, "target_basis": basis,
           "inflation_used_pct": round(infl_used * 100, 2), "corpus_at_start_p50": float(np.median(corpus_start)),
           "outflow_nominal": outflow_nom_p50, "mix_end": {k: round(float(v), 3) for k, v in zip(simulate.ASSET_ORDER, weights[-1])},
           "years": round(years, 1), "scenario": ctx.why.get("scenario")}
    out.update(extra or {})
    return out


def _expected(a: Assumptions, mix: dict) -> float:
    """Expected yearly return of a mix after tax, in percent (what the goal card shows as its return)."""
    tot = sum(mix.values()) or 1.0
    return round(sum(v * a.after_tax(k) for k, v in mix.items()) / tot * 100, 2)


def _quantile(x: np.ndarray, q: float) -> float:
    return float(np.quantile(x, q))


def plan_retirement(db: Session, ctx: Context, goal, corpus0: float, sip_now_by_owner: dict[str, float], items: list,
                    typed_today: float, use_typed: bool, retire_ages: dict[str, int] | None = None, search: bool = True) -> Optional[dict]:
    """One household retirement: it starts when the first earner stops (their profile's retirement age); a spouse still working offsets
    spending until their own retirement date; the money lasts until the chance that either spouse is alive falls to 15% (life table,
    app/planning/longevity.py), with spending stepping down once it is more likelier than not that one has died."""
    today, a0 = ctx.today, ctx.base
    owners = {o: dict(v, retirement_age=(retire_ages or {}).get(o, v["retirement_age"])) for o, v in ctx.owners.items() if v["dob"] and v["retirement_age"]}
    if not owners:
        return None
    ret_dates = {o: _add_years(v["dob"], v["retirement_age"]) for o, v in owners.items()}
    first = min(ret_dates, key=lambda o: ret_dates[o])
    start_date = ret_dates[first]
    life = longevity.household_horizon([v["dob"] for v in ctx.owners.values() if v["dob"]], today)
    horizon_end = life["end"]
    n_acc, n = _months(today, start_date), _months(today, horizon_end)
    if n <= n_acc:
        return None
    step_m = _months(today, life["step"]) if life["step"] else n + 1
    years = n_acc / 12
    spend_today = ctx.spending["retirement_core"]
    a, tax_note = _goal_assumptions(ctx, items, years)
    mix_vals = allocation.blend([(v, w) for _o, v, w in items])
    start = mix_vals or {"equity": 0.7, "debt": 0.3}
    glide = min(10.0, years)
    w = simulate.glide_weights(start, RETIRE_MIX, n, round(glide * 12), glide_end=n_acc)
    # contributions: each person's SIP runs until their own retirement month
    streams = []
    for o, amt in sip_now_by_owner.items():
        if amt > 0 and o in ret_dates:
            streams.append((amt, _months(today, ret_dates[o])))
    total_sip = sum(a_ for a_, _ in streams)
    if not streams:
        streams = [(1.0, n_acc)]
        total_sip = 0.0
    norm = sum(a_ for a_, _ in streams)
    shape = simulate.contribution_shape(n, ctx.stepup, [(a_ / norm, m_) for a_, m_ in streams])
    # a spouse still earning offsets spending until their retirement month (a haircut for job risk; salary grows)
    haircut = float(param("earnings", "income_haircut")["value"])
    offset = np.zeros(n + 1)
    for o, v in owners.items():
        if o == first:
            continue
        m_end = _months(today, ret_dates[o])
        inc_m = ctx.salary.get(o, 0.0) * haircut / 12
        if inc_m > 0 and m_end > n_acc:
            mm = np.arange(n_acc, min(m_end, n))
            offset[mm] += inc_m * (1 + a.salary_growth) ** (mm / 12)

    household = np.where(np.arange(n + 1) < step_m, 1.0, life["factor"])

    def outflow(infl, n_paths):
        o = np.zeros((n_paths, n + 1))
        o[:, n_acc:n] = np.maximum(spend_today / 12 * household[None, n_acc:n] * infl["basket"][:, n_acc:n] - offset[None, n_acc:n], 0.0)
        return o

    res = simulate.simulate_plan(corpus0=corpus0, n_months=n, shape=shape, scale_now=total_sip, outflow_fn=outflow, weights=w,
                                 retire_month=n_acc, data=ctx.data, a=a, confidences=(0.5, CONFIDENCE, 0.9))
    D = res["D"]
    o_nom = outflow(res["infl"], a.n_paths)
    req = (o_nom[:, n_acc:n] * D[:, n_acc:n] / D[:, n_acc:n_acc + 1]).sum(axis=1)
    deflate = (1 + a.inflation_general) ** years
    req80 = _quantile(req, CONFIDENCE)
    ideal_today = req80 / deflate
    target_today = typed_today if use_typed else ideal_today
    extra = {"expected_return_pct": _expected(a, start), "required_at_start_nominal": req80, "start_date": start_date.isoformat(), "horizon_end": horizon_end.isoformat(),
             "spend_today": spend_today, "spouse_offset_today_monthly": {o: ctx.salary.get(o, 0.0) * haircut / 12 for o in owners if o != first},
             "tax": tax_note, "first_retiree": first, "retire_dates": {o: d.isoformat() for o, d in ret_dates.items()},
             "mix_start": {k: round(v, 3) for k, v in start.items()}, "step_date": life["step"].isoformat() if life["step"] else None,
             "step_factor": life["factor"], "survivor_chance": life["chance"]}
    out = _result(ctx, a, res, shape, total_sip, corpus0, n_acc, req80, ideal_today, "typed" if use_typed else "derived",
                  a.retirement_inflation, years, w, extra)
    if use_typed:                                           # chance against the typed corpus rather than the derived need
        c_start = (corpus0 + total_sip * np.cumsum(shape[None, :] * D, axis=1)[:, n_acc]) / D[:, n_acc]
        out["chance"] = float((c_start >= typed_today * deflate).mean())
    if search and out["chance"] is not None and out["chance"] < CONFIDENCE and total_sip > 0:
        out["age_for_80"] = _retire_age_for_chance(db, ctx, goal, corpus0, sip_now_by_owner, items, typed_today, use_typed, owners[first]["retirement_age"], first)
    return out


def _retire_age_for_chance(db, ctx, goal, corpus0, sip_by_owner, items, typed_today, use_typed, from_age: int, who: str, top: int = 70) -> Optional[int]:
    """Earliest retirement age (of the first earner) at which today's SIPs reach CONFIDENCE; None if not even by `top`. Chance only rises with age."""
    light = replace(ctx, base=replace(ctx.base, n_paths=min(1000, ctx.base.n_paths)))            # a search needs the ranking of ages, not the third decimal of a chance

    def ok(age: int) -> bool:
        r = plan_retirement(db, light, goal, corpus0, sip_by_owner, items, typed_today, use_typed, retire_ages={who: age}, search=False)
        return bool(r and r["chance"] is not None and r["chance"] >= CONFIDENCE)
    if not ok(top):
        return None
    lo, hi = from_age + 1, top
    while lo < hi:
        mid = (lo + hi) // 2
        lo, hi = (lo, mid) if ok(mid) else (mid + 1, hi)
    return lo


def plan_staged(db: Session, ctx: Context, goal, corpus0: float, sip_now: float, items: list, outflows: list[tuple[date, float, str]],
                category_rate: float, goal_type: str, years_to_first: float, typed_today: float, use_typed: bool, basis_default: str,
                fallback: list[tuple[date, float, str]] | None = None, decumulate: bool = False) -> Optional[dict]:
    """A goal paid in one or more dated instalments (today's money, inflated per category), funded by a SIP until the first payment.
    `decumulate`: payments run for decades after the first (a lifetime cost): the money moves to the retirement mix and its tax drag."""
    today = ctx.today
    if not outflows:
        return None
    months = [_months(today, d) for d, _amt, _cat in outflows]
    n = max(months)
    first_out = min(months)
    a, tax_note = _goal_assumptions(ctx, items, years_to_first)
    owner_vals: dict[str, float] = {}
    for o, v, _w in items:
        owner_vals[o] = owner_vals.get(o, 0.0) + v
    risk = _risk_shift(ctx, owner_vals) if owner_vals else "moderate"
    rec_start = allocation.split_for(allocation.recommended_equity(years_to_first, risk, goal_type), goal_type)
    start = allocation.blend([(v, w_) for _o, v, w_ in items]) or rec_start
    end = RETIRE_MIX if decumulate else allocation.split_for(allocation.recommended_equity(0.0, risk, goal_type), goal_type)
    w = simulate.glide_weights(start, end, n, round(min(10.0 if decumulate else 5.0, years_to_first) * 12), glide_end=first_out)
    shape = simulate.contribution_shape(n, ctx.stepup, [(1.0, first_out)])
    scale = {"general": a.inflation_general}

    def mk(schedule, scale_typed):
        def outflow(infl, n_paths):
            o = np.zeros((n_paths, n + 1))
            for d, amt, cat in schedule:
                m = _months(today, d)
                o[:, m] += amt * scale_typed * infl[cat][:, m]
            return o
        return outflow

    derived_total = sum(amt for _d, amt, _c in outflows)
    k = (typed_today / derived_total) if (use_typed and derived_total > 0 and not decumulate) else 1.0
    outflow = mk(outflows, k)
    res = simulate.simulate_plan(corpus0=corpus0, n_months=n, shape=shape, scale_now=sip_now, outflow_fn=outflow, weights=w,
                                 retire_month=first_out if decumulate else None, data=ctx.data, a=a, confidences=(0.5, CONFIDENCE, 0.9))
    D = res["D"]
    o_nom = outflow(res["infl"], a.n_paths)
    req = (o_nom[:, first_out:] * D[:, first_out:] / D[:, first_out:first_out + 1]).sum(axis=1)
    req80 = _quantile(req, CONFIDENCE)
    total_nom_p50 = float(np.median(o_nom.sum(axis=1)))
    ideal_today = float(derived_total)
    if decumulate:                                       # a lifetime cost: the corpus needed when it starts, in the cost's own prices today
        ideal_today = req80 / float(np.median(res["infl"][outflows[0][2]][:, first_out]))
        if use_typed and ideal_today > 0 and abs(typed_today / ideal_today - 1) > 1e-9:
            return plan_staged(db, ctx, goal, corpus0, sip_now, items, [(d, amt * typed_today / ideal_today, c) for d, amt, c in outflows],
                               category_rate, goal_type, years_to_first, typed_today, False, basis_default, fallback, decumulate) | {"target_basis": "typed", "ideal_target_today": typed_today}
    extra = {"expected_return_pct": _expected(a, start), "tax": tax_note, "mix_start": {k_: round(v, 3) for k_, v in start.items()}, "mix_recommended_now": {k_: round(v, 3) for k_, v in rec_start.items()},
             "mix_recommended_end": {k_: round(v, 3) for k_, v in end.items()}, "required_at_start_nominal": req80, "payments": len(outflows),
             "first_payment": min(d for d, _a, _c in outflows).isoformat(), "last_payment": max(d for d, _a, _c in outflows).isoformat()}
    out = _result(ctx, a, res, shape, sip_now, corpus0, first_out, total_nom_p50, ideal_today, "typed" if use_typed else basis_default,
                  category_rate, years_to_first, w, extra)
    if fallback:
        fb = simulate.simulate_plan(corpus0=corpus0, n_months=max(_months(today, d) for d, _a, _c in fallback), shape=simulate.contribution_shape(
            max(_months(today, d) for d, _a, _c in fallback), ctx.stepup, [(1.0, min(_months(today, d) for d, _a, _c in fallback))]),
            scale_now=sip_now, outflow_fn=_fallback_outflow(today, fallback), weights=simulate.glide_weights(
                start, end, max(_months(today, d) for d, _a, _c in fallback), round(min(5.0, years_to_first) * 12),
                glide_end=min(_months(today, d) for d, _a, _c in fallback)), retire_month=None, data=ctx.data, a=a, confidences=(CONFIDENCE,))
        out["fallback_chance"] = fb["probability"]
        out["fallback_today"] = float(sum(amt for _d, amt, _c in fallback))
    return out


def health_schedule(ctx: Context) -> list[tuple[date, float, str]]:
    """Senior health cost (today's money): from each person's retirement (the employer's cover ends) to their own life-table horizon, a year at a
    time: an individual Rs 10 lakh policy plus the bills the NSS hospitalisation rate and LASI out-of-pocket cost imply (app/planning/params.yml)."""
    p = lambda k: float(param("health", k)["value"])
    per_year = p("senior_premium_per_person") + p("senior_hospitalisation_rate") * p("out_of_pocket_per_hospitalisation")
    out = []
    for o in ctx.owners.values():
        if not o["dob"] or not o["retirement_age"]:
            continue
        start, end = _add_years(o["dob"], o["retirement_age"]), longevity.person_end(o["dob"], ctx.today)
        d = start
        while d < end:
            out.append((d, per_year, "medical"))
            d = _add_years(d, 1)
    return sorted(out)


def _fallback_outflow(today: date, schedule):
    n = max(_months(today, d) for d, _a, _c in schedule)

    def outflow(infl, n_paths):
        o = np.zeros((n_paths, n + 1))
        for d, amt, cat in schedule:
            m = _months(today, d)
            o[:, m] += amt * infl[cat][:, m]
        return o
    return outflow


def education_schedule(dob: date, abroad: bool) -> list[tuple[date, float, str]]:
    """Tuition paid at the start of each academic year from the child's age at entry (params.yml), in today's money, inflated by category."""
    out = []
    for kind in ("ug", "pg"):
        p = param("costs_today", f"education_{'abroad' if abroad else 'india'}_{kind}_total")
        per_year = p["value"] / p["years"]
        for i in range(int(p["years"])):
            out.append((_add_years(dob, int(p["age"]) + i), per_year, "education_abroad" if abroad else "education"))
    return out


def plan_emergency(ctx: Context, corpus0: float, typed_today: float, use_typed: bool) -> dict:
    """Months of spending the family cannot cut: more when one earner carries the household and a loan runs."""
    inc = {o: ctx.salary.get(o, 0.0) for o in ctx.owners}
    top_share = (max(inc.values()) / sum(inc.values())) if sum(inc.values()) > 0 else 1.0
    months = float(param("emergency", "months_single_income_with_loan" if (ctx.loan and top_share >= 0.7) else "months_floor")["value"])
    monthly = ctx.spending["total"] / 12
    ideal = months * monthly
    target = typed_today if use_typed else ideal
    return {"chance": None, "ideal_target_today": ideal, "target_basis": "typed" if use_typed else "derived",
            "months": months, "monthly_spending": monthly, "sip_for_80": max(0.0, target - corpus0) / 12, "sip_for_90": max(0.0, target - corpus0) / 12,
            "inflation_used_pct": None, "years": 0.0, "outflow_nominal": target, "corpus_at_start_p50": corpus0, "scenario": ctx.why.get("scenario"),
            "note": f"{months:.0f} months of spending (one earner carries the household and a loan runs)" if months > 6 else "6 months of spending"}


def _note(res: dict, goal_type: str, ctx: Context) -> str:
    if goal_type == "retirement":
        off = res.get("spouse_offset_today_monthly") or {}
        txt = (f"Household retirement from {res['start_date']}; money lasts to {res['horizon_end'][:4]} (until the chance either of you is alive falls to "
               f"{res['survivor_chance'] * 100:.0f}%, official life table)" + (f", spending {int((1 - res['step_factor']) * 100)}% lower from {res['step_date'][:4]}" if res.get("step_date") else "")
               + f". Living cost ₹{res['spend_today'] / 100000:.1f} L/yr (today's money, health is in its own goal)"
               + (f"; a working spouse's income (₹{sum(off.values()) / 1000:.0f}k/mo after a job-risk haircut) offsets it until their own retirement." if off else "."))
        if res.get("age_for_80"):
            txt += f" At today's saving an 80% chance needs retiring at {res['age_for_80']}."
        elif res.get("chance") is not None and res["chance"] < CONFIDENCE and "age_for_80" in res:
            txt += " At today's saving, even retiring at 70 does not reach an 80% chance."
        return txt
    if goal_type == "health":
        return (f"Senior health from {res['first_payment'][:4]} (each of you from your own retirement) to {res['last_payment'][:4]}: a ₹10 L individual policy plus expected bills, "
                f"needs ₹{res['ideal_target_today'] / 100000:.0f} L when it starts (today's prices; cost rising {res['inflation_used_pct']}% a year at first, fading). "
                f"{'Planning on your figure.' if res['target_basis'] == 'typed' else 'Catastrophic illness is not in it.'}")
    if goal_type == "child_education":
        fb = res.get("fallback_chance")
        return (f"Abroad: undergraduate + postgraduate paid yearly from {res['first_payment'][:7]} to {res['last_payment'][:7]} (₹{res['ideal_target_today'] / 10000000:.2f} cr today's money, "
                f"cost rising {res['inflation_used_pct']}% a year)" + (f". The India route (₹{res['fallback_today'] / 100000:.0f} L) is covered in {fb * 100:.0f}% of paths." if fb is not None else "."))
    if goal_type == "emergency":
        return res.get("note", "")
    return f"Your figure ₹{res['ideal_target_today'] / 100000:.0f} L today's money, rising {res['inflation_used_pct']}% a year."


def overlay(db: Session, goal, proj, sip_by_owner: dict[str, float], scope: list[str], ctx: Context) -> bool:
    """Replace a goal's deterministic projection by the planner's, field for field. False when the goal type has no plan (custom)."""
    from app.engine.goal_returns import goal_tags
    gt = (goal.goal_type or "").lower()
    if gt in ("custom", ""):
        return False
    from dataclasses import replace as _replace
    manual_step = getattr(goal, "sip_stepup_pct", None)
    if manual_step is not None:                              # a yearly rise typed on the goal wins over the assumed one
        ctx = _replace(ctx, stepup=float(manual_step) / 100)
    corpus0 = float(proj.current_corpus or 0)
    typed = float(goal.target_amount or 0)
    use_typed = bool(getattr(goal, "use_manual_return", False))
    key = (str(goal.id), round(corpus0, -3), tuple(sorted((o, round(v, -2)) for o, v in sip_by_owner.items())), typed, use_typed,
           ctx.stepup, ctx.today, ctx.why.get("scenario"), str(proj.target_date))
    hit = _RESULTS.get(key)
    if hit and time.time() - hit[0] < _TTL:
        res = hit[1]
    else:
        items = holdings_by_owner(db, goal_tags(goal), scope)
        sip_now = sum(sip_by_owner.values())
        res = None
        if gt == "retirement":
            res = plan_retirement(db, ctx, goal, corpus0, sip_by_owner, items, typed, use_typed)
        elif gt == "child_education":
            from app.engine.goals import _load_children_from_config
            dobs = []
            for c in _load_children_from_config():
                v = c.get("dob")
                try:
                    dobs.append(v if isinstance(v, date) else date.fromisoformat(str(v)))
                except ValueError:
                    pass
            if dobs:
                dob = min(dobs)
                a = ctx.base
                res = plan_staged(db, ctx, goal, corpus0, sip_now, items, education_schedule(dob, True), a.inflation_education_abroad, gt,
                                  (min(education_schedule(dob, True))[0] - ctx.today).days / 365.25, typed, use_typed, "derived",
                                  fallback=education_schedule(dob, False))
        elif gt == "health":
            sched = health_schedule(ctx)
            if sched:
                res = plan_staged(db, ctx, goal, corpus0, sip_now, items, sched, ctx.base.inflation_medical, gt,
                                  max(0.25, (sched[0][0] - ctx.today).days / 365.25), typed, use_typed, "derived", decumulate=True)
        elif gt == "child_marriage":
            when = proj.target_date or goal.target_date
            res = plan_staged(db, ctx, goal, corpus0, sip_now, items, [(when, typed, "wedding")], ctx.base.inflation_wedding, gt,
                              max(0.25, (when - ctx.today).days / 365.25), typed, True, "typed")
        elif gt in ("emergency", "emergency_fund"):
            res = plan_emergency(ctx, corpus0, typed, use_typed)
        if res is None:
            return False
        _RESULTS[key] = (time.time(), res)
    if gt in ("emergency", "emergency_fund"):
        target_now = typed if use_typed else res["ideal_target_today"]
        proj.inflation_adj_target = round(target_now, 2)
        proj.monthly_sip_needed = round(res["sip_for_80"], 2)
        proj.gap = round(corpus0 - target_now, 2)
        proj.on_track = corpus0 >= target_now
    else:
        proj.inflation_adj_target = round(res["required_at_start_nominal"], 2)
        proj.monthly_sip_needed = round(res["sip_for_80"] or 0.0, 2) if res["sip_for_80"] is not None else proj.monthly_sip_needed
        proj.projected_corpus = round(res["corpus_at_start_p50"], 2)
        proj.gap = round(proj.projected_corpus - proj.inflation_adj_target, 2)
        proj.on_track = res["chance"] >= CONFIDENCE
        if res.get("start_date"):
            proj.target_date = date.fromisoformat(res["start_date"])
        elif res.get("first_payment"):
            proj.target_date = date.fromisoformat(res["first_payment"])
    proj.chance = round(res["chance"], 3) if res.get("chance") is not None else None
    proj.sip_for_90 = round(res["sip_for_90"], 2) if res.get("sip_for_90") is not None else None
    proj.ideal_target_today = round(res["ideal_target_today"], 2)
    proj.typed_target_today = round(typed, 2)
    proj.target_source = res["target_basis"]
    proj.inflation_used_pct = res.get("inflation_used_pct")
    if res.get("expected_return_pct") is not None:
        proj.effective_return_pct = res["expected_return_pct"]
        proj.return_source = "market"
    if gt not in ("emergency", "emergency_fund"):
        proj.sip_stepup_pct = round(ctx.stepup * 100, 1)
        proj.sip_stepup_source = "set" if manual_step is not None else "assumed"
        proj.glide_active = True                             # every plan moves to the safer mix as its date nears
    proj.plan_note = _note(res, "retirement" if gt == "retirement" else gt, ctx)
    return True
