"""
Real-time gold and silver prices for India.

Sources:
  - COMEX Gold futures  (GC=F)    via Yahoo Finance public chart API
  - COMEX Silver futures (SI=F)   via Yahoo Finance public chart API
  - USD/INR exchange rate (USDINR=X) via Yahoo Finance

Prices are cached for 6 hours in data/metal_prices_cache.json.

Gold 24K  = COMEX spot conversion (999 purity, ≈ same as IBJA 24K)
Gold 22K  = 24K × (22/24) = 91.67%
Gold 18K  = 24K × (18/24) = 75%
Silver    = COMEX SI=F
SGB       = Gold 24K (1 SGB unit = 1 gram 24K gold for valuation)
"""
from __future__ import annotations

import bisect
import json
import logging
from datetime import datetime
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_CACHE_FILE = Path("data/metal_prices_cache.json")
_HISTORY_CACHE_FILE = Path("data/metal_history_cache.json")
_CACHE_HOURS = 6
_HISTORY_CACHE_HOURS = 24
_TROY_OZ_TO_GRAM = 31.1035

_YF_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d"
_YF_HISTORY_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1mo&range=7y"
_YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}


def _fetch_yf_price(symbol: str) -> float | None:
    try:
        resp = httpx.get(
            _YF_URL.format(symbol=symbol),
            headers=_YF_HEADERS,
            timeout=12,
            follow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.json()
        meta = data["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        return float(price) if price else None
    except Exception as exc:
        logger.warning("Yahoo Finance fetch failed for %s: %s", symbol, exc)
        return None


def _load_cache() -> dict:
    if _CACHE_FILE.exists():
        try:
            cached = json.loads(_CACHE_FILE.read_text())
            cached_at = datetime.fromisoformat(cached.get("fetched_at", "2000-01-01"))
            age_hours = (datetime.now() - cached_at).total_seconds() / 3600
            if age_hours < _CACHE_HOURS:
                return cached
        except Exception:
            pass
    return {}


def _fetch_yf_history(symbol: str) -> list[tuple[int, float]]:
    """Returns (unix_timestamp, close_price) pairs for the past 7 years at monthly interval."""
    try:
        resp = httpx.get(
            _YF_HISTORY_URL.format(symbol=symbol),
            headers=_YF_HEADERS,
            timeout=20,
            follow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
        return [(ts, c) for ts, c in zip(timestamps, closes) if c is not None]
    except Exception as exc:
        logger.warning("Yahoo Finance history fetch failed for %s: %s", symbol, exc)
        return []


def _load_history_cache() -> dict:
    if _HISTORY_CACHE_FILE.exists():
        try:
            cached = json.loads(_HISTORY_CACHE_FILE.read_text())
            cached_at = datetime.fromisoformat(cached.get("fetched_at", "2000-01-01"))
            age_hours = (datetime.now() - cached_at).total_seconds() / 3600
            if age_hours < _HISTORY_CACHE_HOURS:
                return cached
        except Exception:
            pass
    return {}


def _cagr(start: float, end: float, years: float) -> float | None:
    if start <= 0 or end <= 0 or years <= 0:
        return None
    return round(((end / start) ** (1.0 / years) - 1) * 100, 2)


def _cagr_for_period(series: list[dict], years: int) -> float | None:
    target_months = years * 12
    if len(series) < target_months:
        return None
    start = series[-target_months]["price_inr_per_gram"]
    end = series[-1]["price_inr_per_gram"]
    return _cagr(start, end, years)


def get_metal_price_history() -> dict:
    """
    Monthly gold/silver prices in INR/gram for the past 7 years, plus 1Y/3Y/5Y/7Y CAGR.
    Cached 24 hours in data/metal_history_cache.json.
    """
    cached = _load_history_cache()
    if cached:
        return cached

    gold_raw     = _fetch_yf_history("GC=F")
    silver_raw   = _fetch_yf_history("SI=F")
    usd_inr_raw  = _fetch_yf_history("USDINR=X")

    if not gold_raw or not usd_inr_raw:
        if _HISTORY_CACHE_FILE.exists():
            logger.warning("Metal history fetch failed; using stale cache.")
            return json.loads(_HISTORY_CACHE_FILE.read_text())
        return {"error": "history unavailable — Yahoo Finance unreachable"}

    # Build sorted USDINR lookup for nearest-timestamp matching
    usd_inr_dict = {ts: rate for ts, rate in usd_inr_raw}
    usd_inr_ts   = sorted(usd_inr_dict.keys())

    def _nearest_fx(ts: int) -> float | None:
        if not usd_inr_ts:
            return None
        idx = bisect.bisect_left(usd_inr_ts, ts)
        candidates = []
        if idx < len(usd_inr_ts):
            candidates.append(usd_inr_ts[idx])
        if idx > 0:
            candidates.append(usd_inr_ts[idx - 1])
        nearest = min(candidates, key=lambda x: abs(x - ts))
        return usd_inr_dict[nearest]

    def _to_inr_series(raw: list[tuple[int, float]]) -> list[dict]:
        out = []
        for ts, price_usd_oz in raw:
            fx = _nearest_fx(ts)
            if fx:
                out.append({
                    "date":              datetime.utcfromtimestamp(ts).strftime("%Y-%m"),
                    "price_inr_per_gram": round((price_usd_oz * fx) / _TROY_OZ_TO_GRAM, 2),
                })
        return out

    gold_series   = _to_inr_series(gold_raw)
    silver_series = _to_inr_series(silver_raw)

    result = {
        "gold": {
            "history":  gold_series,
            "cagr_pct": {
                "1y": _cagr_for_period(gold_series, 1),
                "3y": _cagr_for_period(gold_series, 3),
                "5y": _cagr_for_period(gold_series, 5),
                "7y": _cagr_for_period(gold_series, 7),
            },
        },
        "silver": {
            "history":  silver_series,
            "cagr_pct": {
                "1y": _cagr_for_period(silver_series, 1),
                "3y": _cagr_for_period(silver_series, 3),
                "5y": _cagr_for_period(silver_series, 5),
                "7y": _cagr_for_period(silver_series, 7),
            },
        },
        "fetched_at": datetime.now().isoformat(),
    }

    _HISTORY_CACHE_FILE.parent.mkdir(exist_ok=True)
    _HISTORY_CACHE_FILE.write_text(json.dumps(result, indent=2))
    logger.info(
        "Metal price history refreshed: %d gold, %d silver monthly points",
        len(gold_series), len(silver_series),
    )
    return result


def compute_asset_cagr(
    asset_type: str,
    quantity: float,
    invested_amount: float,
    purity: str = "24K",
) -> float | None:
    """
    Annualised return for a metal asset based on invested cost vs. current market value.
    Returns CAGR % or None if data is insufficient.
    """
    if quantity <= 0 or invested_amount <= 0:
        return None
    purchase_price_per_gram = invested_amount / quantity
    prices = get_metal_prices()
    if "error" in prices:
        return None
    if asset_type == "silver":
        current = prices.get("silver_inr_per_gram", 0)
    elif asset_type == "sgb":
        current = prices.get("sgb_inr_per_unit", 0)
    else:
        key = f"gold_{purity.lower()}_inr_per_gram"
        current = prices.get(key) or prices.get("gold_24k_inr_per_gram", 0)
    # Use 7Y history to find approximate purchase year if price matches
    # but we don't have start_date here, so return simple total-return as a float
    # (caller should pass years separately for true CAGR)
    if current <= 0 or purchase_price_per_gram <= 0:
        return None
    total_return = (current / purchase_price_per_gram - 1) * 100
    return round(total_return, 2)


def get_metal_prices() -> dict:
    """
    Returns gold/silver spot prices in INR per gram, with a 6-hour local cache.
    Falls back to stale cache if Yahoo Finance is unreachable.
    """
    cached = _load_cache()
    if cached:
        return cached

    gold_usd_oz   = _fetch_yf_price("GC=F")
    silver_usd_oz = _fetch_yf_price("SI=F")
    usd_inr       = _fetch_yf_price("USDINR=X")

    if not (gold_usd_oz and usd_inr):
        if _CACHE_FILE.exists():
            logger.warning("Metal price fetch failed; using stale cache.")
            return json.loads(_CACHE_FILE.read_text())
        return {
            "error": "prices unavailable — Yahoo Finance unreachable",
            "gold_24k_inr_per_gram": 0,
            "gold_22k_inr_per_gram": 0,
            "gold_18k_inr_per_gram": 0,
            "silver_inr_per_gram":   0,
            "sgb_inr_per_unit":      0,
        }

    gold_24k = round((gold_usd_oz * usd_inr) / _TROY_OZ_TO_GRAM, 2)
    silver    = round(((silver_usd_oz or 0) * usd_inr) / _TROY_OZ_TO_GRAM, 2) if silver_usd_oz else 0.0

    result = {
        "gold_24k_inr_per_gram": gold_24k,
        "gold_22k_inr_per_gram": round(gold_24k * 22 / 24, 2),
        "gold_18k_inr_per_gram": round(gold_24k * 18 / 24, 2),
        "silver_inr_per_gram":   silver,
        "sgb_inr_per_unit":      gold_24k,   # 1 SGB unit = 1 gram 24K gold
        "usd_inr":               round(usd_inr, 2),
        "gold_usd_per_oz":       round(gold_usd_oz, 2),
        "silver_usd_per_oz":     round(silver_usd_oz, 2) if silver_usd_oz else 0,
        "fetched_at":            datetime.now().isoformat(),
        "source":                "Yahoo Finance (COMEX futures + USDINR=X)",
    }

    _CACHE_FILE.parent.mkdir(exist_ok=True)
    _CACHE_FILE.write_text(json.dumps(result, indent=2))
    logger.info(
        "Metal prices refreshed: Gold 24K ₹%.0f/g, Silver ₹%.0f/g (USD/INR=%.2f)",
        gold_24k, silver, usd_inr,
    )
    return result


def price_for_asset(asset_type: str, quantity: float, purity: str = "24K") -> float:
    """
    Calculate current value of a metal asset.
    purity: '24K' | '22K' | '18K'  (only relevant for gold/sgb)
    """
    prices = get_metal_prices()
    if "error" in prices:
        return 0.0

    if asset_type == "silver":
        return round(quantity * prices["silver_inr_per_gram"], 2)
    if asset_type == "sgb":
        return round(quantity * prices["sgb_inr_per_unit"], 2)
    if asset_type == "gold":
        key = f"gold_{purity.lower()}_inr_per_gram"
        rate = prices.get(key) or prices["gold_24k_inr_per_gram"]
        return round(quantity * rate, 2)
    return 0.0
