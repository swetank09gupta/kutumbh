"""
Fetch latest India CPI inflation from the World Bank Open Data API.
Cached locally for 24 hours so the app stays fast and works offline.
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
_DEFAULT     = 6.0   # fallback when API is unreachable

# World Bank: India CPI annual inflation %, last 3 data points
_URL = (
    "https://api.worldbank.org/v2/country/IN/indicator/FP.CPI.TOTL.ZG"
    "?format=json&mrv=3"
)


def get_india_cpi() -> float:
    """Return latest India annual CPI inflation rate (%). 24-hour cached."""
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

    if _CACHE_FILE.exists():
        try:
            cached = json.loads(_CACHE_FILE.read_text())
            age    = datetime.now() - datetime.fromisoformat(cached["cached_at"])
            if age < timedelta(hours=_CACHE_HOURS):
                return float(cached["rate"])
        except Exception:
            pass

    try:
        with urllib.request.urlopen(_URL, timeout=6) as resp:
            data = json.loads(resp.read())
        entries = data[1] if len(data) > 1 else []
        rate = next(
            (float(e["value"]) for e in entries if e.get("value") is not None),
            _DEFAULT,
        )
        _CACHE_FILE.write_text(json.dumps({
            "rate":      rate,
            "cached_at": datetime.now().isoformat(),
        }))
        logger.info("India CPI refreshed: %.2f%%", rate)
        return rate
    except Exception as exc:
        logger.warning("CPI fetch failed (%s) — using %.1f%%", exc, _DEFAULT)
        return _DEFAULT
