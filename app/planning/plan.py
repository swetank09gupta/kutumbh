"""
The plan: today's facts (from the app) + real history + simulation → per goal and for the household's retirement. docs/PLANNING.md.
Derived values are shown beside yours; nothing typed in the app is overwritten.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.planning import allocation, history, need, simulate
from app.planning.assumptions import SCENARIOS, Assumptions, scenario

DEFAULT_STEPUP = 0.08                    # yearly rise of savings (salary growth), a stated assumption; yours is shown as "detected"
RETIRE_MIX = {"equity": 0.40, "gold": 0.05, "debt": 0.55}
RETIRE_AGES = (50, 55, 60)
HORIZON_AGES = (85, 95)
MIN_YEARS = 0.25


def _f(x) -> float:
    return float(x) if isinstance(x, (Decimal, int, float)) else 0.0


def age_on(born: date | None, on: date) -> float | None:
    return round((on - born).days / 365.25, 2) if born else None


def goal_mix(db: Session, tags: list[str], owner_ids: list[str]) -> tuple[dict, float]:
    """(mix, value) of the holdings and manual assets tagged to a goal."""
    from app.engine.holdings_view import latest_holdings
    from app.models.schemas import ManualAsset
    items = []
    for h in latest_holdings(db, owner_ids=owner_ids):
        if h.goal_tag in tags:
            items.append((_f(h.current_value), allocation.classify(h.instrument_name, h.holding_type)))
    for m in db.query(ManualAsset).filter(ManualAsset.owner_id.in_(owner_ids), ManualAsset.goal_tag.in_(tags)).all():
        items.append((_f(m.current_value), allocation.classify(m.name, None, m.asset_type)))
    return allocation.blend(items), sum(v for v, _ in items)


def facts(db: Session, today: date) -> dict:
    sp = need.household_spending(db, today)
    nw = db.execute(text("SELECT bank_balance, equity_value, mf_value, ppf_value, fd_value, real_estate_value, other_assets, total_liabilities, "
                         "net_worth, snapshot_date FROM net_worth_snapshots WHERE owner_id = 'family' ORDER BY snapshot_date DESC LIMIT 1")).first()
    loan = db.execute(text("SELECT lender, as_on, principal_after, emi, rate, emis_left FROM loan_anchors ORDER BY as_on DESC LIMIT 1")).first()
    out = {"spending": sp, "income_12m": sp["income"], "spending_12m": sp["total"],
           "savings_rate": (sp["income"] - sp["total"]) / sp["income"] if sp["income"] else None}
    if nw:
        fin = sum(_f(getattr(nw, k)) for k in ("bank_balance", "equity_value", "mf_value", "ppf_value", "fd_value", "other_assets"))
        out["net_worth"] = {"as_of": str(nw.snapshot_date), "bank": _f(nw.bank_balance), "equity": _f(nw.equity_value), "mf": _f(nw.mf_value),
                            "ppf": _f(nw.ppf_value), "fd": _f(nw.fd_value), "property": _f(nw.real_estate_value), "other": _f(nw.other_assets),
                            "liabilities": _f(nw.total_liabilities), "net_worth": _f(nw.net_worth), "financial_assets": fin,
                            "financial_minus_loan": fin - _f(nw.total_liabilities)}
    if loan:
        out["loan"] = {"lender": loan.lender, "as_on": str(loan.as_on), "outstanding": _f(loan.principal_after), "emi": _f(loan.emi),
                       "rate_pct": _f(loan.rate), "emis_left": int(loan.emis_left or 0)}
    return out


def _accumulation(g, p, data, a, scope, risk, today, stepup, db) -> dict:
    years = max(MIN_YEARS, (g.target_date - today).days / 365.25)
    mix, value = goal_mix(db, allocation_tags(g), scope)
    rec_eq = allocation.recommended_equity(years, risk, g.goal_type)
    rec_start = allocation.split_for(rec_eq, g.goal_type)
    end = allocation.split_for(allocation.recommended_equity(0.0, risk, g.goal_type), g.goal_type)
    start = mix or rec_start
    infl_yours = _f(g.inflation_rate) / 100 if g.inflation_rate is not None else a.inflation_general
    data_infl = {"child_education": a.inflation_education, "health": a.inflation_medical}.get(g.goal_type, a.inflation_general)
    common = dict(corpus0=_f(p.current_corpus), sip0=_f(p.current_monthly_sip), stepup=stepup, years=years, target_today=_f(g.target_amount),
                  end_weights=end, glide_years=min(5.0, years), data=data, a=a)
    now = simulate.simulate_accumulation(goal_inflation=infl_yours, start_weights=start, **common)
    out = {"name": g.name, "type": g.goal_type, "target_date": str(g.target_date), "years": round(years, 1), "target_today": _f(g.target_amount),
           "corpus_now": _f(p.current_corpus), "sip_now": _f(p.current_monthly_sip),
           "inflation_yours_pct": round(infl_yours * 100, 2), "inflation_data_pct": round(data_infl * 100, 2),
           "mix_now": {k: round(v, 3) for k, v in start.items()}, "mix_recommended_now": {k: round(v, 3) for k, v in rec_start.items()},
           "mix_recommended_end": {k: round(v, 3) for k, v in end.items()},
           "at_your_plan": now, "options": _options(g, common, infl_yours, start, a)}
    if abs(data_infl - infl_yours) > 0.004:
        out["with_data_inflation"] = simulate.simulate_accumulation(goal_inflation=data_infl, start_weights=start, **common)
    if mix and rec_start and abs(mix.get("equity", 0) - rec_start["equity"]) > 0.15:
        out["if_mix_followed_guidance"] = simulate.simulate_accumulation(goal_inflation=infl_yours, start_weights=rec_start, **common)
    return out


# Cost ladders from public sources (docs/PLANNING.md §2, RetireWise 2026): today's money, to be re-based by the owner, never auto-applied.
OPTIONS = {
    "child_education": [("India: IIT/IIM/private (≈₹28L)", 2_800_000), ("Abroad undergraduate (≈₹60L)", 6_000_000),
                        ("Abroad MBA (≈₹1.1 cr)", 11_000_000), ("Abroad degree + MBA (≈₹1.7 cr)", 17_000_000)],
    "child_marriage": [("Modest (₹25L)", 2_500_000), ("Moderate (₹50L)", 5_000_000)],
    "health": [("Cover the gap beyond insurance (₹25L)", 2_500_000), ("Senior care + major events (₹1 cr)", 10_000_000)],
}


def _options(g, kw, infl, start, a) -> list[dict]:
    out = []
    ladder = list(OPTIONS.get(g.goal_type, [])) + [("Your target", _f(g.target_amount))]
    for label, tgt in ladder:
        kw2 = dict(kw, target_today=tgt)
        now = simulate.simulate_accumulation(goal_inflation=infl, start_weights=start, **kw2)
        out.append({"label": label, "target_today": tgt, "probability_at_current": now["probability"],
                    "sip_for_80": now["sip_for_confidence"]["80"], "sip_for_90": now["sip_for_confidence"]["90"]})
    return out


def loan_payoff(outstanding: float, annual_rate_pct: float, emi: float, extra: float = 0.0, max_months: int = 600) -> dict:
    """Months to clear a reducing-balance loan paying emi + extra a month; total interest."""
    r, bal, interest = annual_rate_pct / 1200, outstanding, 0.0
    for m in range(1, max_months + 1):
        i = bal * r
        interest += i
        bal = bal + i - (emi + extra)
        if bal <= 0.005:                                   # under half a paisa is floating-point residue, not a balance
            return {"months": m, "interest": interest, "paid": True}
    return {"months": max_months, "interest": interest, "paid": False}


def prepay_vs_invest(data, a, rate_pct: float, years=(5, 10, 15)) -> list[dict]:
    """The loan's rate is a certain return. How often does the market beat it (after tax, on the real history), and how badly
    can it fall short? Equity, a 60/40 mix, and debt, over each horizon."""
    import numpy as np
    rate = rate_pct / 100
    mixes = {"100% equity": {"equity": 1.0}, "60/40 equity/debt": {"equity": 0.6, "debt": 0.4}, "100% debt": {"debt": 1.0}}
    out = []
    for y in years:
        n = y * 12
        rng = np.random.default_rng(a.seed)
        dev, mu, _ = simulate.calibrate(data["joint"], data["equity_long"], a)
        r3 = simulate.draw_returns(dev, mu, a.n_paths, n, a.block_months, rng)
        row = {"years": y, "rate_pct": rate_pct, "mixes": {}}
        for label, w in mixes.items():
            wts = simulate.glide_weights(w, w, n, 0)
            fin = simulate.grow(simulate.portfolio_returns(r3, wts, a.after_tax("ppf")), 1.0, 0.0, 0.0)
            cagr = fin ** (1 / y) - 1
            row["mixes"][label] = {"beats_loan": float((cagr >= rate).mean()), "cagr_p10": float(np.percentile(cagr, 10)),
                                   "cagr_p50": float(np.percentile(cagr, 50)), "worst_shortfall_p10": float(max(0.0, rate - np.percentile(cagr, 10)))}
        out.append(row)
    return out


def capacity(fx: dict, extra_prepay_options=(0, 100_000, 200_000, 300_000)) -> dict:
    sp, loan = fx["spending"], fx.get("loan")
    surplus = (sp["income"] - sp["total"]) / 12
    out = {"income_month": sp["income"] / 12, "spending_month": sp["total"] / 12, "surplus_before_loan_prepay_month": surplus,
           "note": "Recurring only: income minus spending over the last 12 months. Unlabelled credits are not counted."}
    if loan:
        emi = loan["emi"]
        out["loan"] = {**loan, "surplus_after_loan_month": surplus + emi,
                       "payoff": [{"extra_prepay_month": x, **loan_payoff(loan["outstanding"], loan["rate_pct"], emi, x)} for x in extra_prepay_options]}
    return out


def allocation_tags(g) -> list[str]:
    from app.engine.goal_returns import goal_tags
    return goal_tags(g)


def household_retirement(db, data, a, today, stepup, spend_today, owners) -> dict:
    from app.models.schemas import Goal
    from app.engine.goals import project_all_goals
    goals = {gl.name: gl for gl in db.query(Goal).filter(Goal.goal_type == "retirement").all()}
    proj = {p.name: p for p in project_all_goals(db)}
    scope = [o["id"] for o in owners]
    parts = [(n, gl) for n, gl in goals.items()]
    corpus = sum(_f(proj[n].current_corpus) for n, _ in parts)
    sip = sum(_f(proj[n].current_monthly_sip) for n, _ in parts)
    mix, _ = goal_mix(db, ["retirement", "wife_retirement"], scope)
    start = mix or {"equity": 0.7, "debt": 0.3}
    target_sum = sum(_f(gl.target_amount) for _, gl in parts)
    user = next((o for o in owners if o["id"] == "user"), owners[0])
    base_age = user["age"]
    grid = []
    for ra in RETIRE_AGES:
        for to in HORIZON_AGES:
            y0, y1 = ra - base_age, to - ra
            if y0 <= 0 or y1 <= 0:
                continue
            r = simulate.simulate_retirement(corpus0=corpus, sip0=sip, stepup=stepup, years_to_retire=y0, years_in_retirement=y1,
                                             spend_today=spend_today, start_weights=start, retire_weights=RETIRE_MIX, glide_years=min(10.0, y0),
                                             data=data, a=a)
            grid.append({"retire_at_age": ra, "to_age": to, **r})
    views = {}
    for name in SCENARIOS:                                  # the same question under a cautious, a base and a hopeful market
        an = scenario(name, a)
        rows = []
        for ra in (55, 60):
            y0, y1 = ra - base_age, 95 - ra
            if y0 <= 0:
                continue
            r = simulate.simulate_retirement(corpus0=corpus, sip0=sip, stepup=stepup, years_to_retire=y0, years_in_retirement=y1,
                                             spend_today=spend_today, start_weights=start, retire_weights=RETIRE_MIX, glide_years=min(10.0, y0),
                                             data=data, a=an)
            rows.append({"retire_at_age": ra, "to_age": 95, "probability": r["probability"],
                         "required_today_money_90": r["required_corpus_today_money"]["90"], "sip_for_90": r["sip_for_confidence"]["90"]})
        views[name] = rows
    return {"corpus_now": corpus, "sip_now": sip, "targets_you_set_sum_today": target_sum, "spend_today": spend_today, "by_market": views,
            "mix_now": {k: round(v, 3) for k, v in start.items()}, "retire_mix": RETIRE_MIX, "user_age": base_age,
            "scenarios": grid, "components": [n for n, _ in parts]}


def build(db: Session, scenario_name: str = "base", stepup: float = DEFAULT_STEPUP, today: date | None = None,
          spend_today: float | None = None, refresh_history: bool = False) -> dict:
    from app.engine.goals import project_all_goals
    from app.engine.networth import real_owners
    from app.models.schemas import Goal
    today = today or date.today()
    a = scenario(scenario_name)
    data = history.load(refresh=refresh_history, today=today)
    fx = facts(db, today)
    owners = [{"id": o.id, "name": o.name, "age": age_on(o.birth_date, today), "risk": o.risk_appetite, "retirement_age": o.retirement_age}
              for o in real_owners(db)]
    scope_all = [o["id"] for o in owners]
    risk = (next((o for o in owners if o["id"] == "user"), owners[0]))["risk"]
    proj = {p.goal_id: p for p in project_all_goals(db)}
    goals_out = []
    for g in db.query(Goal).order_by(Goal.target_date).all():
        p = proj.get(str(g.id))
        if p is None or g.goal_type == "retirement":
            continue
        if g.goal_type == "emergency":
            goals_out.append(_emergency(g, p, fx))
            continue
        scope = scope_all if g.owner_id in (None, "", "family") else [g.owner_id]
        goals_out.append(_accumulation(g, p, data, a, scope, risk, today, stepup, db))
    core = spend_today if spend_today else fx["spending"]["retirement_core"]
    hh = household_retirement(db, data, a, today, stepup, core, owners)
    cap = capacity(fx)
    pv = prepay_vs_invest(data, a, fx["loan"]["rate_pct"]) if fx.get("loan") else None
    return {"as_of": today.isoformat(), "scenario": scenario_name, "scenarios_available": list(SCENARIOS), "stepup_assumed": stepup,
            "assumptions": {k: v for k, v in vars(a).items() if not k.startswith("_")},
            "history": {**data["meta"], "stats": history.stats(data["joint"], data["equity_long"])},
            "facts": fx, "capacity": cap, "prepay_vs_invest": pv, "owners": owners, "goals": goals_out, "household_retirement": hh}


def _emergency(g, p, fx) -> dict:
    monthly = fx["spending"]["total"] / 12 if fx["spending"]["total"] else 0
    corpus = _f(p.current_corpus)
    return {"name": g.name, "type": "emergency", "target_today": _f(g.target_amount), "corpus_now": corpus,
            "monthly_spending": monthly, "months_covered": corpus / monthly if monthly else None,
            "target_months_6": 6 * monthly, "target_months_12": 12 * monthly}
