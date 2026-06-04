"""
Current market-valuation signals for India + global equity.

Pulled from public sources, cached for 12 hours so the app is fast and works
offline. Used by recommendations engine to add "when/where to invest" context.

Sources
-------
* Nifty indices P/E (Nifty 50, Nifty Midcap 150, Nifty Smallcap 250, Nifty 500)
    → niftyindices.com daily snapshot JSON
* 10-yr India G-Sec yield, USD/INR, Nasdaq 100 level, Brent crude
    → Yahoo Finance (`^IRX`-equivalent for India is `^INDIAGS10Y` if available,
      else fall back to investing.com via httpx)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_CACHE_FILE  = Path("data/market_signals_cache.json")
_CACHE_HOURS = 12

# Reasonable historical anchors (~5-yr rolling averages, updated annually).
# Used to decide cheap / fair / expensive when live P/E is fetched.
_HIST_AVG_PE = {
    "nifty50":        22.0,
    "nifty_midcap":   28.0,
    "nifty_smallcap": 27.0,
    "nifty500":       23.0,
    "nasdaq100":      28.0,
}

# Fallback values when the network is unreachable — conservative "fair" stance.
_FALLBACK = {
    "nifty50_pe":        22.5,
    "nifty_midcap_pe":   34.0,
    "nifty_smallcap_pe": 30.0,
    "nifty500_pe":       23.5,
    "nasdaq100_pe":      30.0,
    "ind_10y_yield":     7.10,
    "usd_inr":           84.0,
    "fetched_at":        None,
    "source":            "fallback",
}


def _load_cache() -> Optional[dict]:
    if not _CACHE_FILE.exists():
        return None
    try:
        cached = json.loads(_CACHE_FILE.read_text())
        age = datetime.now() - datetime.fromisoformat(cached["cached_at"])
        if age < timedelta(hours=_CACHE_HOURS):
            return cached["data"]
    except Exception as exc:
        logger.debug("market signals cache read failed: %s", exc)
    return None


def _save_cache(data: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(
            {"cached_at": datetime.now().isoformat(), "data": data},
            indent=2,
        ))
    except Exception as exc:
        logger.debug("market signals cache write failed: %s", exc)


def _fetch_nifty_pe() -> dict:
    """
    Fetch latest P/E for Nifty 50 / Nifty Midcap 150 / Nifty Smallcap 250 /
    Nifty 500 from NSE's daily indices CSV.

    Source: https://archives.nseindia.com/content/indices/ind_close_all_<DDMMYYYY>.csv
    Format: `Index Name,Index Date,Open,High,Low,Close,Pts Chg,%Chg,Vol,Turnover,P/E,P/B,Div Yield`

    Today's file isn't always live (markets must close + NSE must post); we try
    the last 7 calendar days back until we get a populated CSV.
    """
    from datetime import date, timedelta
    out: dict[str, Optional[float]] = {
        "nifty50_pe":        None,
        "nifty_midcap_pe":   None,
        "nifty_smallcap_pe": None,
        "nifty500_pe":       None,
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer":    "https://www.niftyindices.com/",
        "Accept":     "*/*",
    }
    name_map = {
        "nifty 50":           "nifty50_pe",
        "nifty midcap 150":   "nifty_midcap_pe",
        "nifty smallcap 250": "nifty_smallcap_pe",
        "nifty 500":          "nifty500_pe",
    }
    fetched_for_date: Optional[str] = None

    try:
        with httpx.Client(timeout=8.0, headers=headers, follow_redirects=True) as client:
            for days_back in range(1, 8):
                day = (date.today() - timedelta(days=days_back)).strftime("%d%m%Y")
                url = f"https://archives.nseindia.com/content/indices/ind_close_all_{day}.csv"
                try:
                    r = client.get(url)
                except Exception:
                    continue
                if r.status_code != 200 or len(r.text) < 500:
                    continue
                # Parse CSV — first line is header
                lines = r.text.splitlines()
                if not lines or "P/E" not in lines[0]:
                    continue
                # Column index for P/E (handles minor format drift)
                cols = [c.strip() for c in lines[0].split(",")]
                try:
                    pe_idx = cols.index("P/E")
                except ValueError:
                    pe_idx = 10  # historical default
                for ln in lines[1:]:
                    parts = [p.strip() for p in ln.split(",")]
                    if len(parts) <= pe_idx:
                        continue
                    key = name_map.get(parts[0].strip().lower())
                    if not key:
                        continue
                    try:
                        out[key] = float(parts[pe_idx])
                    except (ValueError, IndexError):
                        pass
                # Stop once we got at least one populated index
                if any(v is not None for v in out.values()):
                    fetched_for_date = (date.today() - timedelta(days=days_back)).isoformat()
                    break
    except Exception as exc:
        logger.info("NSE indices CSV fetch failed (%s); using fallback P/E", exc)

    if fetched_for_date:
        logger.info("Nifty P/E fetched from NSE for %s", fetched_for_date)
    out["_data_date"] = fetched_for_date  # surfaced in cache for transparency
    return out


def _fetch_yahoo_quote(symbol: str) -> Optional[float]:
    """Latest close from Yahoo chart API (free, no auth)."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{symbol}?range=5d&interval=1d")
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        with httpx.Client(timeout=6.0, headers=headers) as client:
            r = client.get(url)
            r.raise_for_status()
            j = r.json()
            closes = (j["chart"]["result"][0]["indicators"]["quote"][0]["close"])
            for v in reversed(closes):
                if v is not None:
                    return float(v)
    except Exception as exc:
        logger.debug("yahoo quote %s failed: %s", symbol, exc)
    return None


def _fetch_10y_yield_india() -> Optional[float]:
    """India 10-year G-Sec yield. Investing.com chart proxy via Yahoo isn't
    available; we approximate using the FRED IRLTLT01INM156N monthly series."""
    url = "https://api.stlouisfed.org/fred/series/observations"  # needs API key
    # Without an API key, return None — caller uses fallback.
    return None


def get_market_signals() -> dict:
    """
    Returns a dict of current market valuations + 12-hour cache.

    Keys:
      nifty50_pe, nifty_midcap_pe, nifty_smallcap_pe, nifty500_pe,
      ind_10y_yield, usd_inr, nasdaq100_level, nasdaq100_pe,
      fetched_at, source (live | cached | fallback)

    Additionally derived (not cached):
      *_zone:        "cheap" | "fair" | "expensive" vs historical avg
      midcap_premium_pct:  (midcap_pe - nifty50_pe) / nifty50_pe * 100
    """
    cached = _load_cache()
    if cached:
        return _enrich(cached, source="cached")

    out = dict(_FALLBACK)
    out["fetched_at"] = datetime.now().isoformat()
    out["source"] = "live"

    # Nifty P/Es
    pes = _fetch_nifty_pe()
    data_date = pes.pop("_data_date", None)
    out.update({k: v for k, v in pes.items() if v is not None})
    if data_date:
        out["nifty_data_date"] = data_date

    # USD/INR + Nasdaq 100
    inr = _fetch_yahoo_quote("INR=X")
    if inr:
        out["usd_inr"] = round(inr, 2)
    ndx = _fetch_yahoo_quote("^NDX")
    if ndx:
        out["nasdaq100_level"] = round(ndx, 2)

    # India 10-yr yield (best-effort)
    yld = _fetch_10y_yield_india()
    if yld is not None:
        out["ind_10y_yield"] = yld

    # Mark source = fallback if Nifty fetch returned nothing live
    if all(pes.get(k) is None for k in ("nifty50_pe", "nifty_midcap_pe",
                                          "nifty_smallcap_pe", "nifty500_pe")):
        out["source"] = "fallback"

    _save_cache(out)
    return _enrich(out, source=out.get("source", "live"))


def _zone(pe: Optional[float], hist_avg: float) -> str:
    if pe is None:
        return "unknown"
    ratio = pe / hist_avg
    if ratio < 0.92:
        return "cheap"
    if ratio > 1.10:
        return "expensive"
    return "fair"


def _enrich(d: dict, source: str) -> dict:
    out = dict(d)
    out["source"] = source
    out["nifty50_zone"]        = _zone(out.get("nifty50_pe"),        _HIST_AVG_PE["nifty50"])
    out["nifty_midcap_zone"]   = _zone(out.get("nifty_midcap_pe"),   _HIST_AVG_PE["nifty_midcap"])
    out["nifty_smallcap_zone"] = _zone(out.get("nifty_smallcap_pe"), _HIST_AVG_PE["nifty_smallcap"])
    out["nifty500_zone"]       = _zone(out.get("nifty500_pe"),       _HIST_AVG_PE["nifty500"])
    if out.get("nifty50_pe") and out.get("nifty_midcap_pe"):
        out["midcap_premium_pct"] = round(
            (out["nifty_midcap_pe"] - out["nifty50_pe"]) / out["nifty50_pe"] * 100, 1
        )
    else:
        out["midcap_premium_pct"] = None
    out["hist_avg_pe"] = _HIST_AVG_PE
    return out
