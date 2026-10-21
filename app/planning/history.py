"""
Real monthly returns of the three planning assets, from public month-end NAVs (AMFI via mfapi.in), cached 30 days.

equity = UTI Nifty 50 Index Fund - Regular - Growth (2006→), gold = SBI Gold Fund - Regular - Growth (2011→),
debt = UTI Gilt Fund - Regular - Growth (2006→). Regular-plan NAVs are net of fund costs: what an investor actually received.
The current, incomplete month is dropped. On a fetch failure a stale cache is used (and flagged); with none, PlanningDataUnavailable.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

SERIES = {"equity": 100822, "gold": 115676, "debt": 102510}          # AMFI scheme codes
ASSETS = tuple(SERIES)
TTL_DAYS = 30
API = "https://api.mfapi.in/mf/{code}"


class PlanningDataUnavailable(RuntimeError):
    pass


def _cache_path() -> Path:
    return Path(os.getenv("PLANNING_HISTORY_CACHE", "data/cache/planning_history.json"))


def fetch_nav(code: int) -> dict[str, float]:
    import httpx
    rows = httpx.get(API.format(code=code), timeout=60).json()["data"]
    return {datetime.strptime(r["date"], "%d-%m-%Y").date().isoformat(): float(r["nav"]) for r in rows}


def month_end_returns(navs: dict[str, float], today: date | None = None) -> pd.Series:
    """Monthly returns from month-end NAVs; the month in progress is not a return yet."""
    today = today or date.today()
    s = pd.Series({pd.Timestamp(k): v for k, v in navs.items()}).sort_index()
    m = s.resample("ME").last()
    m = m[m.index < pd.Timestamp(today).replace(day=1)]              # complete months only
    return m.pct_change().dropna()


def frame(navs_by_asset: dict[str, dict[str, float]], today: date | None = None) -> pd.DataFrame:
    """Joint monthly returns over the months all assets share, and each asset's own longer series."""
    return pd.DataFrame({a: month_end_returns(navs_by_asset[a], today) for a in ASSETS}).dropna()


def load(refresh: bool = False, today: date | None = None) -> dict:
    """{"joint": DataFrame (equity, gold, debt monthly returns), "equity_long": Series (20-year equity), "meta": {...}}"""
    today = today or date.today()
    path = _cache_path()
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text())
        except (OSError, ValueError):
            cached = None
    fresh = cached and (today - date.fromisoformat(cached["fetched_on"])).days <= TTL_DAYS
    stale = False
    if refresh or not fresh:
        try:
            navs = {a: fetch_nav(code) for a, code in SERIES.items()}
            cached = {"fetched_on": today.isoformat(), "navs": navs}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cached))
        except Exception as exc:                                     # network down: the last good cache beats nothing
            logger.warning("planning history fetch failed: %s", exc)
            if not cached:
                raise PlanningDataUnavailable(str(exc)) from exc
            stale = True
    joint = frame(cached["navs"], today)
    equity_long = month_end_returns(cached["navs"]["equity"], today)
    return {"joint": joint, "equity_long": equity_long,
            "meta": {"fetched_on": cached["fetched_on"], "stale": stale, "months": len(joint),
                     "from": joint.index[0].date().isoformat(), "to": joint.index[-1].date().isoformat(),
                     "equity_months": len(equity_long), "equity_from": equity_long.index[0].date().isoformat(),
                     "series": {a: SERIES[a] for a in ASSETS}}}


def stats(joint: pd.DataFrame, equity_long: pd.Series) -> dict:
    """Plain facts about the sample, for display and for calibration."""
    cagr = lambda x: float((1 + x).prod() ** (12 / len(x)) - 1)
    return {
        "cagr": {a: cagr(joint[a]) for a in ASSETS}, "cagr_equity_long": cagr(equity_long),
        "vol": {a: float(joint[a].std() * np.sqrt(12)) for a in ASSETS}, "vol_equity_long": float(equity_long.std() * np.sqrt(12)),
        "corr": joint.corr().round(3).to_dict(),
        "worst_equity_12m": float((1 + equity_long).rolling(12).apply(np.prod, raw=True).min() - 1),
    }
