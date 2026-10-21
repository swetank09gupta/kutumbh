"""What each goal's money is actually in (equity / gold / debt / PPF), and what it should be for its horizon."""
from __future__ import annotations

import re
from collections import defaultdict

_DEBT = re.compile(r"gilt|liquid|overnight|money\s*market|ultra\s*short|low\s*duration|short\s*(term|duration)|corporate\s*bond|banking\s*&?\s*psu|"
                   r"floater|dynamic\s*bond|income\s*fund|credit\s*risk|treasury|g-?sec|bond\s*fund|debt|fmp|target\s*maturity", re.I)
_GOLD = re.compile(r"gold|sgb|sovereign\s*gold", re.I)
_HYBRID = re.compile(r"hybrid|balanced|multi\s*asset|arbitrage|equity\s*savings|dynamic\s*asset", re.I)


def classify(name: str | None, holding_type: str | None, asset_type: str | None = None) -> dict[str, float]:
    """Weights over equity/gold/debt/ppf for one holding. Hybrid funds count 60/40 equity/debt (a stated simplification)."""
    n = name or ""
    if asset_type:
        a = asset_type.lower()
        if a == "ppf":
            return {"ppf": 1.0}
        if a in ("fd", "rd", "nps_debt", "insurance"):
            return {"debt": 1.0}
        if a in ("gold", "silver", "sgb"):
            return {"gold": 1.0}
        if a == "nps":
            return {"equity": 0.5, "debt": 0.5}
    if (holding_type or "").lower() == "bonds":                      # ISIN IN0…: sovereign gold bonds, or other government/corporate bonds
        return {"gold": 1.0} if _GOLD.search(n) else {"debt": 1.0}
    if _GOLD.search(n):
        return {"gold": 1.0}
    if _DEBT.search(n):
        return {"debt": 1.0}
    if _HYBRID.search(n):
        return {"equity": 0.6, "debt": 0.4}
    return {"equity": 1.0}


def blend(items: list[tuple[float, dict[str, float]]]) -> dict[str, float]:
    """Value-weighted mix of (value, weights) pairs; {} when there is nothing."""
    tot = sum(v for v, _ in items if v > 0)
    out: dict[str, float] = defaultdict(float)
    for v, w in items:
        if v <= 0:
            continue
        for k, x in w.items():
            out[k] += v * x / tot
    return {k: round(x, 6) for k, x in out.items()}


_CURVE = None                       # set by the planner once the market risk model is built (risk.equity_share); the table below is the fallback


def set_market_curve(fn) -> None:
    global _CURVE
    if fn is None:
        _CURVE = None
        return
    from functools import lru_cache
    cached = lru_cache(maxsize=1024)(fn)
    _CURVE = lambda years: cached(round(float(years), 2))


def recommended_equity(years_to_goal: float, risk: str | None = "moderate", goal_type: str = "") -> float:
    """Equity share suggested for the horizon (a glide path, docs/PLANNING.md): the largest share whose measured 1-in-20 horizon fits inside the years
    left (app/planning/risk.py); a table only until market data has been read. Emergency money is never in equity. Risk appetite moves it by 10 points."""
    if goal_type == "emergency":
        return 0.0
    y = years_to_goal
    if _CURVE is not None:
        base = _CURVE(y)
    else:
        base = 0.75 if y >= 10 else 0.60 if y >= 7 else 0.45 if y >= 5 else 0.30 if y >= 3 else 0.10
    shift = {"conservative": -0.10, "aggressive": 0.10}.get((risk or "moderate").lower(), 0.0)
    return max(0.0, min(0.85, base + shift))


def split_for(equity: float, goal_type: str = "") -> dict[str, float]:
    """Full mix for a target equity share: the rest in debt, with a little gold for long horizons only."""
    gold = 0.05 if equity >= 0.45 else 0.0
    return {"equity": equity, "gold": gold, "debt": max(0.0, 1.0 - equity - gold)}


def goals_equity_share(items: list[tuple[float, float, str]], risk: str | None = "moderate", default: float = 0.60) -> float:
    """Corpus-weighted equity share (0-1) a set of goals calls for: items are (corpus, years to the first payment, goal type)."""
    num = den = 0.0
    for corpus, years, goal_type in items:
        if corpus > 0:
            num += corpus * recommended_equity(max(0.0, years), risk, goal_type)
            den += corpus
    return num / den if den else default
