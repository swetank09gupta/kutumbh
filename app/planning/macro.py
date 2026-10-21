"""
Today's macro picture, assembled from what the app already tracks plus two public series, each with its source and date.

  yield_10y, nifty50_pe, usd_inr   app.engine.market_signals (live, cached 12 h)
  cpi_10y_avg                      app.engine.inflation (World Bank, cached 24 h)
  cpi_latest_yoy, rbi projection   params.yml (dated public figures)
  inr depreciation                 USD/INR from FRED (DEXINUS), cached 7 days: the 10-year compound change of the rupee
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

PARAMS = Path(__file__).with_name("params.yml")
FRED_USDINR = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DEXINUS"
FX_TTL_DAYS = 7
HIST_AVG_PE = 22.0                      # the app's own long-run anchor for the Nifty 50 (market_signals._HIST_AVG_PE)


def params() -> dict:
    return yaml.safe_load(PARAMS.read_text())


def param(group: str, key: str, today: date | None = None):
    """The latest dated entry of a parameter (value; the entry carries source and verified_on)."""
    rows = params()[group][key]
    return max(rows, key=lambda r: str(r.get("verified_on", ""))) if isinstance(rows, list) else rows


def _fx_cache() -> Path:
    return Path(os.getenv("PLANNING_FX_CACHE", "data/cache/planning_fx.json"))


def fetch_usdinr() -> dict[str, float]:
    """Monthly average USD/INR since the series' start: {'YYYY-MM': rate}."""
    import httpx
    text = httpx.get(FRED_USDINR, timeout=60, follow_redirects=True).text
    sums: dict[str, list[float]] = {}
    for line in text.splitlines()[1:]:
        d, _, v = line.partition(",")
        try:
            sums.setdefault(d[:7], []).append(float(v))
        except ValueError:
            continue                                          # FRED prints "." for a day without a quote
    return {k: sum(v) / len(v) for k, v in sums.items()}


def usdinr_history(today: date | None = None) -> dict[str, float]:
    today = today or date.today()
    path = _fx_cache()
    try:
        c = json.loads(path.read_text())
        if (today - date.fromisoformat(c["fetched_on"])).days <= FX_TTL_DAYS:
            return c["rates"]
    except (OSError, ValueError, KeyError):
        c = None
    try:
        rates = fetch_usdinr()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_on": today.isoformat(), "rates": rates}))
        return rates
    except Exception as exc:
        logger.warning("USD/INR history fetch failed: %s", exc)
        if c:
            return c["rates"]
        return {}


def inr_depreciation_pct(rates: dict[str, float], years: int = 10) -> float | None:
    """Compound yearly rise of USD/INR over the last `years` (the rupee losing value against the dollar), in percent."""
    if len(rates) < 24:
        return None
    last = max(rates)
    y, m = int(last[:4]) - years, last[5:7]
    start = f"{y}-{m}"
    if start not in rates:
        earlier = [k for k in rates if k >= start]
        if not earlier:
            return None
        start = min(earlier)
    n = (int(last[:4]) - int(start[:4])) + (int(last[5:7]) - int(start[5:7])) / 12
    return ((rates[last] / rates[start]) ** (1 / n) - 1) * 100 if n > 1 else None


def snapshot(today: date | None = None, signals: dict | None = None, cpi: dict | None = None, fx: dict | None = None) -> dict:
    """Everything the expectations are derived from, with provenance. Arguments let tests (and callers that already hold them) skip fetching."""
    today = today or date.today()
    if signals is None:
        from app.engine import market_signals
        signals = market_signals.get_market_signals()
    if cpi is None:
        from app.engine import inflation
        cpi = inflation.get_india_cpi_stats()
    rates = fx if fx is not None else usdinr_history(today)
    dep = inr_depreciation_pct(rates)
    latest = param("inflation", "cpi_latest_yoy")
    proj = param("inflation", "rbi_cpi_projection_fy27")
    return {
        "as_of": today.isoformat(),
        "yield_10y": signals.get("ind_10y_yield"), "nifty50_pe": signals.get("nifty50_pe"), "nifty50_pe_avg": HIST_AVG_PE,
        "usd_inr": signals.get("usd_inr") or (rates[max(rates)] if rates else None), "signals_source": signals.get("source"),
        "cpi_10y_avg": cpi.get("avg_10y"), "cpi_latest_year": cpi.get("latest"), "cpi_latest_yoy": latest["value"],
        "cpi_latest_yoy_as_of": latest["as_of"], "rbi_cpi_projection": proj["value"],
        "inr_depreciation_10y_pct": dep,
    }
