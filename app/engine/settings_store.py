"""App-wide assumptions, stored once in app_settings (not repeated per goal)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.schemas import AppSetting

DEFAULT_INFLATION = 6.0       # % a year, one rate for every goal and projection


def get_inflation(db: Session) -> float:
    row = db.get(AppSetting, "inflation_rate")
    try:
        return float(row.value) if row else DEFAULT_INFLATION
    except ValueError:
        return DEFAULT_INFLATION


def set_inflation(db: Session, rate: float) -> float:
    if not (0.0 <= rate <= 20.0):
        raise ValueError("inflation must be between 0 and 20 %")
    row = db.get(AppSetting, "inflation_rate")
    if row is None:
        db.add(AppSetting(key="inflation_rate", value=str(rate)))
    else:
        row.value = str(rate)
    db.commit()
    return rate
