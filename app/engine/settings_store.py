"""
App-wide inflation fallback, derived from data: the 10-year average of India's annual CPI (World Bank), which is what a 15-25 year goal should assume, not
last year's single print. Planned goals use their own per-category rates (app/planning); this is the rate for a goal the planner does not cover.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

DEFAULT_INFLATION = 6.0       # only if the World Bank has never been reachable


def get_inflation(db: Session | None = None) -> float:
    from app.engine.inflation import get_india_cpi_stats
    return round(float(get_india_cpi_stats()["avg_10y"] or DEFAULT_INFLATION), 1)
