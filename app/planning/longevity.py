"""
How long the money must last, from the official life table (docs/PLANNING.md §3) instead of a typed age.

Survival comes from the SRS 2016-20 urban female table (the longer-lived sex, so no sex is assumed), conditional on each person's age
today; beyond 85, where the table stops, deaths follow a Gompertz tail fitted to the table's own life expectancy at 85. Plans run until the
chance that anyone covered is still alive falls to `plan_until_survivor_chance`; once it is more likely than not that one of two spouses has died,
household spending steps down by `survivor_spending_factor`.
"""
from __future__ import annotations

import math
from datetime import date
from functools import lru_cache

import numpy as np

from app.planning.macro import param

MAX_AGE = 110.0


def _table() -> tuple[dict[int, float], float]:
    t = param("longevity", "table_urban_female_srs_2016_20")
    return {int(k): float(v) for k, v in t["lx"].items()}, float(t["e85"])


@lru_cache(maxsize=8)
def _tail_h0(slope: float, e85: float) -> float:
    """Gompertz h(t) = h0·exp(slope·t) after 85 with mean remaining life e85 (bisection on h0)."""
    ts = np.linspace(0, 40, 4001)

    def mean_life(h0: float) -> float:
        cum = h0 * (np.exp(slope * ts) - 1) / slope
        return float(np.trapezoid(np.exp(-cum), ts))
    lo, hi = 1e-6, 2.0
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if mean_life(mid) > e85 else (lo, mid)
    return (lo + hi) / 2


def survival_from_birth(age: float) -> float:
    """lx(age) / 100000: log-linear between the table's ages, Gompertz beyond 85."""
    lx, e85 = _table()
    ages = sorted(lx)
    if age <= ages[-1]:
        for a0, a1 in zip(ages, ages[1:]):
            if a0 <= age <= a1:
                f = (age - a0) / (a1 - a0)
                return math.exp((1 - f) * math.log(lx[a0]) + f * math.log(lx[a1])) / lx[0]
    slope = float(param("longevity", "old_age_hazard_slope")["value"])
    h0 = _tail_h0(slope, e85)
    t = age - ages[-1]
    return lx[ages[-1]] / lx[0] * math.exp(-h0 * (math.exp(slope * t) - 1) / slope)


def survives(age_now: float, age_then: float) -> float:
    """Chance someone aged `age_now` is alive at `age_then` (a later age)."""
    if age_then <= age_now:
        return 1.0
    return survival_from_birth(age_then) / survival_from_birth(age_now)


def _age(dob: date, on: date) -> float:
    return (on - dob).days / 365.25


def years_until(dobs: list[date], today: date, chance: float, kind: str) -> float:
    """Years from today until P(anyone alive) ('any') or P(everyone alive) ('both') falls to `chance`, for people born on `dobs`."""
    for k in range(0, int(MAX_AGE * 4)):
        y = k / 4
        alive = [survives(_age(d, today), _age(d, today) + y) for d in dobs]
        p = 1 - math.prod(1 - s for s in alive) if kind == "any" else math.prod(alive)
        if p <= chance:
            return y
    return MAX_AGE


def _add_years(d: date, years: float) -> date:
    days = round(years * 365.25)
    return date.fromordinal(d.toordinal() + days)


def household_horizon(dobs: list[date], today: date) -> dict:
    """{'end': last day money must last, 'step': date after which spending is scaled, 'factor': that scale, 'chance': plan chance}."""
    chance = float(param("longevity", "plan_until_survivor_chance")["value"])
    factor = float(param("longevity", "survivor_spending_factor")["value"])
    end = _add_years(today, years_until(dobs, today, chance, "any"))
    step = _add_years(today, years_until(dobs, today, 0.5, "both")) if len(dobs) > 1 else None
    return {"end": end, "step": step, "factor": factor if step else 1.0, "chance": chance}


def person_end(dob: date, today: date) -> date:
    """Last day one person's own cost (their health cover) runs: until their own survival chance falls to `plan_until_survivor_chance`."""
    chance = float(param("longevity", "plan_until_survivor_chance")["value"])
    return _add_years(today, years_until([dob], today, chance, "any"))
