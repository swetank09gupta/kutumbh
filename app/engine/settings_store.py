"""
App-wide assumptions, stored once in app_settings (not repeated per goal).

Inflation is automatic: the 10-year average of India's annual CPI (World Bank), which is what a
15-25 year goal should assume — not last year's single print. A manual override can be set and
cleared; with no override the number follows the data.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.schemas import AppSetting

DEFAULT_INFLATION = 6.0       # only if the World Bank has never been reachable
_OVERRIDE_KEY = "inflation_override"


def inflation_override(db: Session) -> float | None:
    row = db.get(AppSetting, _OVERRIDE_KEY)
    try:
        return float(row.value) if row else None
    except ValueError:
        return None


def get_inflation(db: Session) -> float:
    manual = inflation_override(db)
    if manual is not None:
        return manual
    from app.engine.inflation import get_india_cpi_stats
    return round(float(get_india_cpi_stats()["avg_10y"] or DEFAULT_INFLATION), 1)


def set_inflation(db: Session, rate: float) -> float:
    if not (0.0 <= rate <= 20.0):
        raise ValueError("inflation must be between 0 and 20 %")
    row = db.get(AppSetting, _OVERRIDE_KEY)
    if row is None:
        db.add(AppSetting(key=_OVERRIDE_KEY, value=str(rate)))
    else:
        row.value = str(rate)
    db.commit()
    return rate


def clear_inflation_override(db: Session) -> None:
    row = db.get(AppSetting, _OVERRIDE_KEY)
    if row is not None:
        db.delete(row)
        db.commit()
