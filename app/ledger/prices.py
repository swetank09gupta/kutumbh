"""
Current prices for roll-forward valuation.

  mutual funds / ETFs (INF…)  AMFI NAV (already cached by app.engine.amfi_isin)
  equities (INE…)             Yahoo Finance chart API, NSE ticker, cached for the day
  fallback                    last traded price in the ledger, then the statement rate

Every price comes back with its source and as-of date so holdings rows say how
fresh their valuation is.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

import httpx

from app.engine.amfi_isin import get_nav

logger = logging.getLogger(__name__)

_CACHE_FILE = Path("data/cache/equity_prices.json")
_YF_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json"}


def _load() -> dict:
    try:
        data = json.loads(_CACHE_FILE.read_text())
        return data if data.get("day") == str(date.today()) else {"day": str(date.today()), "p": {}}
    except Exception:
        return {"day": str(date.today()), "p": {}}


def _save(cache: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(cache))
    except Exception as exc:
        logger.debug("equity price cache write failed: %s", exc)


def _yahoo_ticker(symbol: Optional[str]) -> Optional[str]:
    """Contract-note symbols are NSE tickers (GRASIM, HDFCBANK, HDFCGOLD); statement names are not."""
    if not symbol:
        return None
    s = symbol.strip().upper()
    if not s or " " in s or "-" in s and not s.endswith("-E"):
        return None
    return f"{s}.NS"


def equity_price(symbol: Optional[str], client: httpx.Client | None = None) -> Optional[tuple[Decimal, date]]:
    ticker = _yahoo_ticker(symbol)
    if not ticker:
        return None
    cache = _load()
    hit = cache["p"].get(ticker)
    if hit:
        return Decimal(str(hit[0])), date.fromisoformat(hit[1])
    own = client is None
    client = client or httpx.Client(timeout=5.0, headers=_HEADERS, follow_redirects=True)
    try:
        r = client.get(_YF_URL.format(symbol=ticker))
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        meta = res["meta"]
        px = meta.get("regularMarketPrice")
        ts = meta.get("regularMarketTime")
        if not px:
            return None
        as_of = datetime.fromtimestamp(ts).date() if ts else date.today()
        cache["p"][ticker] = [float(px), str(as_of)]
        _save(cache)
        return Decimal(str(px)), as_of
    except Exception as exc:
        logger.debug("yahoo price failed for %s: %s", ticker, exc)
        return None
    finally:
        if own:
            client.close()


def price_for(isin: str, symbol: Optional[str], last_trade: Optional[Decimal],
              last_trade_date: Optional[date], stmt_rate: Optional[Decimal],
              stmt_date: Optional[date], client: httpx.Client | None = None
              ) -> Optional[tuple[Decimal, str, Optional[date]]]:
    """(price, source, as_of) — best available, never raises."""
    if isin.startswith("INF"):
        nav = get_nav(isin)
        if nav:
            return Decimal(f"{nav:.4f}"), "amfi", date.today()
    elif isin.startswith("INE"):
        got = equity_price(symbol, client)
        if got:
            return got[0], "yahoo", got[1]
    # fall back to whichever of (last trade, statement rate) is newer
    cands = []
    if last_trade and last_trade_date:
        cands.append((last_trade_date, last_trade, "last_trade"))
    if stmt_rate and stmt_date:
        cands.append((stmt_date, stmt_rate, "statement"))
    if cands:
        d, p, src = max(cands, key=lambda c: c[0])
        return p, src, d
    return None
