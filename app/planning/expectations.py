"""
Expected returns and inflation by category, derived from live data (docs/PLANNING.md §2). Nothing here is typed in the app.

  debt      10-year G-sec yield less fund cost
  equity    debt + the net equity premium the real history paid over gilts, scaled by valuation (cheaper than the long-run P/E: a
            little more; dearer: less), never below a floor
  gold      general inflation + part of gold's real return since 2011 (capped: the sample holds the 2025-26 spike)
  PPF       the declared rate
  inflation general = mean of the 10-year CPI average, the latest year-on-year print and the RBI's own projection; each category is
            general plus a sourced spread, or (education abroad) US tuition inflation plus the rupee's 10-year fall against the dollar
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import numpy as np

from app.planning.assumptions import Assumptions
from app.planning.macro import HIST_AVG_PE, param

SCENARIO_SHIFT = {  # percentage points
    "cautious": {"equity": -2.0, "gold": -1.0, "debt": -0.5, "inflation": +1.0, "medical_spread": +1.0},
    "base": {"equity": 0.0, "gold": 0.0, "debt": 0.0, "inflation": 0.0, "medical_spread": 0.0},
    "hopeful": {"equity": +1.5, "gold": +0.5, "debt": +0.5, "inflation": -0.5, "medical_spread": 0.0},
}


def _cagr(r) -> float:
    r = np.asarray(r, dtype=float)
    return float((1 + r).prod() ** (12 / len(r)) - 1)


def equity_premium(data: dict) -> float:
    """What equity earned over gilts, net of fund costs, on the months both series share (about 20 years), in points a year."""
    eq, de = data["equity_long"], data["debt_long"]
    idx = eq.index.intersection(de.index)
    return (_cagr(eq.loc[idx]) - _cagr(de.loc[idx])) * 100


def derive(macro: dict, data: dict, scenario: str = "base", ppf: float | None = None, base: Assumptions | None = None) -> tuple[Assumptions, dict]:
    sh = SCENARIO_SHIFT[scenario]
    p = lambda g, k: float(param(g, k)["value"])
    general_inputs = [x for x in (macro.get("cpi_10y_avg"), macro.get("cpi_latest_yoy"), macro.get("rbi_cpi_projection")) if x is not None]
    general = (sum(general_inputs) / len(general_inputs) if general_inputs else 5.5) + sh["inflation"]
    medical = general + p("inflation", "medical_spread_pp") + sh["medical_spread"]
    edu_dom = general + p("inflation", "education_domestic_spread_pp")
    dep = macro.get("inr_depreciation_10y_pct")
    edu_abroad = p("inflation", "us_tuition_inflation") + (dep if dep is not None else 3.5) + (sh["inflation"] if sh["inflation"] > 0 else 0)
    wedding = general + p("inflation", "wedding_spread_pp")

    y10 = macro.get("yield_10y") or 7.1
    debt = y10 - p("returns", "fund_cost_pp") + sh["debt"]
    prem = max(equity_premium(data), p("returns", "equity_premium_floor_pp"))
    pe, pe_avg = macro.get("nifty50_pe"), macro.get("nifty50_pe_avg") or HIST_AVG_PE
    valuation = float(np.clip((pe_avg / pe) ** 0.5, 0.7, 1.25)) if pe else 1.0
    equity = (y10 - p("returns", "fund_cost_pp")) + prem * valuation + sh["equity"]
    cagr_gold = _cagr(data["joint"]["gold"]) * 100
    real_gold = float(np.clip(cagr_gold - (macro.get("cpi_10y_avg") or general), 0.0, None)) * p("returns", "gold_real_haircut")
    gold = general + min(real_gold, p("returns", "gold_real_cap_pp")) + sh["gold"]

    a = replace(base or Assumptions(),
                equity=equity / 100, gold=gold / 100, debt=debt / 100, ppf=(ppf if ppf is not None else 0.071),
                inflation_general=general / 100, inflation_medical=medical / 100, inflation_education=edu_dom / 100,
                inflation_education_abroad=edu_abroad / 100, inflation_wedding=wedding / 100,
                medical_weight_in_retirement=p("inflation", "retirement_medical_weight"),
                salary_growth=p("earnings", "salary_growth_pct") / 100)
    why = {"inflation_general_inputs": general_inputs, "equity_premium_pp": round(prem, 2), "valuation_factor": round(valuation, 3),
           "pe": pe, "pe_avg": pe_avg, "yield_10y": y10, "gold_real_used_pp": round(min(real_gold, p("returns", "gold_real_cap_pp")), 2),
           "inr_depreciation_10y_pct": dep, "scenario": scenario}
    return a, why
