"""
India CPI inflation from the World Bank Open Data API, cached locally for 24 hours.

Two numbers, because they answer different questions:
  latest   the most recent calendar-year print (2025: 2.4%, an unusually low year) — what prices did lately
  avg_10y  the average of the last ten annual prints (~4.7%) — what to expect over a 15-25 year goal

Goal projections use each goal's own inflation rate; neither number overrides it.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_FILE  = Path("data/inflation_cache.json")
_CACHE_HOURS = 24
_DEFAULT     = {"latest": 6.0, "latest_year": None, "avg_10y": 6.0}   # when the API is unreachable

_URL = ("https://api.worldbank.org/v2/country/IN/indicator/FP.CPI.TOTL.ZG"
        "?format=json&mrv=10")


def parse_series(data) -> dict:
    """World Bank payload -> {latest, latest_year, avg_10y}."""
    entries = [(e["date"], float(e["value"])) for e in (data[1] if len(data) > 1 else [])
               if e.get("value") is not None]
    if not entries:
        return dict(_DEFAULT)
    entries.sort(reverse=True)
    return {"latest": round(entries[0][1], 2), "latest_year": entries[0][0],
            "avg_10y": round(sum(v for _, v in entries) / len(entries), 2)}


def get_india_cpi_stats() -> dict:
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _CACHE_FILE.exists():
        try:
            cached = json.loads(_CACHE_FILE.read_text())
            age = datetime.now() - datetime.fromisoformat(cached["cached_at"])
            if age < timedelta(hours=cached.get("ttl_hours", _CACHE_HOURS)) and "avg_10y" in cached:
                return {k: cached[k] for k in ("latest", "latest_year", "avg_10y")}
        except Exception:
            pass
    try:
        with urllib.request.urlopen(_URL, timeout=6) as resp:
            stats = parse_series(json.loads(resp.read()))
        _CACHE_FILE.write_text(json.dumps({**stats, "cached_at": datetime.now().isoformat()}))
        logger.info("India CPI refreshed: %s", stats)
        return stats
    except Exception as exc:
        # Offline: keep the last real figures (annual data barely moves), else the planning default,
        # and retry in an hour rather than once per goal per request.
        logger.warning("CPI fetch failed (%s) — using the last known figures", exc)
        try:
            last = json.loads(_CACHE_FILE.read_text())
            stats = {k: last[k] for k in ("latest", "latest_year", "avg_10y")}
        except Exception:
            stats = dict(_DEFAULT)
        try:
            _CACHE_FILE.write_text(json.dumps({**stats, "cached_at": datetime.now().isoformat(), "ttl_hours": 1}))
        except Exception:
            pass
        return stats


def get_india_cpi() -> float:
    """Latest annual print (kept for callers that display it)."""
    return get_india_cpi_stats()["latest"]
