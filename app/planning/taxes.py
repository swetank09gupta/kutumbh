"""
Tax on returns, per person (docs/PLANNING.md §2): each holder's regime and past earnings set the rate on debt income; equity and gold pay
capital-gains tax at the rule registry's rates, and the horizon decides how much of the compounding that costs. Replaces a flat drag.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.engine import tax_slabs
from app.knowledge import rules
from app.planning.macro import params

CESS = 0.04
RETIREMENT_REGIME = "new"                 # low retirement income, no deductions: the new regime is what a retiree would choose
RETIRED_GAIN_SHARE = 0.5                  # share of a withdrawal from an equity holding that is gain (taxed), a stated simplification


def surcharge(taxable: float, regime: str) -> float:
    rate = 0.0
    for row in sorted(params()["tax"]["surcharge"], key=lambda r: r["above"]):
        if taxable > row["above"]:
            rate = row["rate"] / 100
    return min(rate, 0.25) if regime == "new" else rate


def marginal_rate(taxable: float, fy: str, regime: str) -> float:
    """Tax on the next rupee of ordinary income (slab band, surcharge, cess), as a fraction. 0 when the year or regime is unknown."""
    r = tax_slabs.regime_rules(fy, regime)
    if r is None:
        return 0.0
    band = 0.0
    for upper, rate in r["slabs"]:
        band = rate / 100
        if upper is None or taxable <= upper:
            break
    if taxable <= float(r["rebate"]["limit"]):
        return 0.0                                          # inside the rebate: no tax on the margin
    return band * (1 + surcharge(taxable, regime)) * (1 + CESS)


def ltcg_rate(on: date) -> float:
    return float(rules.value("cg.equity.ltcg_rate", on)) / 100 * (1 + CESS)


def terminal_drag(gross: float, years: float, rate: float) -> float:
    """Yearly return lost when the gain is taxed once at the end: g - ((1 + (1-t)((1+g)^N - 1))^(1/N) - 1)."""
    if years <= 0 or rate <= 0:
        return 0.0
    after = (1 + (1 - rate) * ((1 + gross) ** years - 1)) ** (1 / years) - 1
    return max(0.0, gross - after)


def person_profiles(db, today: date) -> dict[str, dict]:
    """{owner: {regime, taxable_income, marginal_rate, source}}: the highest annualised taxable income of the last three years (past earnings: salary is as
    credited, after the employer's TDS, so a floor) or, where config/accounts.yml gives the current pay (`income`), that pay less the employer's PF share and the
    standard deduction, whichever is higher; taxed at today's slabs in the owner's own regime."""
    from app.engine import tax_view
    from app.planning import personal
    cfg = personal.config()
    employer_pf = {e["owner"]: e["employer_monthly"] * 12 for e in personal.epf_entries(cfg)}
    out = {}
    try:
        built = tax_view.build(db, today)
    except Exception:
        return out
    fy = built["fy"]
    for owner, v in built["owners"].items():
        best = 0.0
        for e in (v.get("estimates") or [])[:3]:
            if not e:
                continue
            months = max(1, int(e.get("salary_months_seen") or 12))
            ann = float(e["taxable_ordinary_income"]) * (12 / months if months < 12 else 1.0)
            best = max(best, ann)
        pay = personal.income(owner, cfg)
        src = "credits"
        if pay:                                              # the headline pay is what the slab sees, not the bank credit after TDS and EPF
            std = float(tax_slabs.standard_deduction(fy, v["regime"]) or 0)
            est = max(0.0, pay["ctc_annual"] - employer_pf.get(owner, 0.0) - std)          # the employer's PF share is not taxed
            if est > best:
                best, src = est, "ctc"
        out[owner] = {"regime": v["regime"], "taxable_income": best, "marginal_rate": marginal_rate(best, fy, v["regime"]), "fy": fy, "source": src}
    return out


def weighted_debt_rate(profiles: dict[str, dict], weights: dict[str, float]) -> float:
    """Marginal rate on debt income for a goal funded by these owners (weights by value); the top profile if nothing is known."""
    tot = sum(w for o, w in weights.items() if o in profiles)
    if tot <= 0:
        return max((p["marginal_rate"] for p in profiles.values()), default=0.3 * (1 + CESS))
    return sum(profiles[o]["marginal_rate"] * w for o, w in weights.items() if o in profiles) / tot


def drags(a, years: float, debt_rate: float, retire_income_today: float, on: date, fy: str) -> tuple[dict, dict]:
    """(saving-years drag, retired drag) by asset, in return points as fractions.

    Saving: debt pays `debt_rate` on its income every year; equity pays LTCG once at the end (so the drag depends on the horizon); gold
    half of that (the SGB part is exempt at maturity); PPF none.
    Retired: debt is taxed at the new-regime marginal rate on `retire_income_today` (today's money: slabs are not indexed to a 2042
    income); equity pays LTCG on the gain share of each withdrawal."""
    t = ltcg_rate(on)
    eq = terminal_drag(a.equity, max(years, 1.0), t)
    saving = {"equity": eq, "gold": terminal_drag(a.gold, max(years, 1.0), t) * 0.5, "debt": a.debt * debt_rate, "ppf": 0.0}
    ret_debt = marginal_rate(retire_income_today, fy, RETIREMENT_REGIME)
    retired = {"equity": a.equity * t * RETIRED_GAIN_SHARE, "gold": a.gold * t * RETIRED_GAIN_SHARE * 0.5, "debt": a.debt * ret_debt, "ppf": 0.0}
    return saving, retired
