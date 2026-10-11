"""
Mutual fund NAV on a past date: AMFI scheme code (from the NAVAll master) + mfapi.in history.

Used only to put a price on units that the depository shows but no contract note/Coin mail
explains (app.ledger.derive). History is cached per scheme under data/cache/nav/ so a refresh
costs no network once a fund has been seen.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_DIR = Path("data/cache/nav")
_URL = "https://api.mfapi.in/mf/{code}"
_SERIES: dict[str, dict[date, Decimal]] = {}


def scheme_code(isin: str) -> Optional[str]:
    from app.engine.amfi_isin import _fetch_navall, _load_cache, _save_cache
    cached = _load_cache()
    entries = cached["entries"] if cached and cached.get("entries") else None
    if entries is None:
        try:
            entries = _fetch_navall()
            _save_cache(entries)
        except Exception as exc:
            logger.warning("AMFI master unavailable: %s", exc)
            return None
    for e in entries:
        if e.get("isin") == isin:
            return str(e["scheme_code"])
    return None


def _parse(rows: list[dict]) -> dict[date, Decimal]:
    out: dict[date, Decimal] = {}
    for r in rows:
        try:
            out[datetime.strptime(r["date"], "%d-%m-%Y").date()] = Decimal(str(r["nav"]))
        except (KeyError, ValueError, ArithmeticError):
            continue
    return out


def series(isin: str, need: Optional[date] = None) -> dict[date, Decimal]:
    """{date: NAV}. Re-fetched when the cache does not reach `need`."""
    s = _SERIES.get(isin)
    if s and (need is None or max(s) >= need):
        return s
    code = scheme_code(isin)
    if not code:
        return {}
    f = _DIR / f"{code}.json"
    if f.exists() and (need is None or True):
        try:
            s = _parse(json.loads(f.read_text()))
            if s and (need is None or max(s) >= need or (date.today() - max(s)).days <= 3):
                _SERIES[isin] = s
                return s
        except Exception:
            pass
    try:
        r = httpx.get(_URL.format(code=code), timeout=30, follow_redirects=True)
        r.raise_for_status()
        rows = r.json().get("data", [])
        _DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(rows))
        s = _parse(rows)
    except Exception as exc:
        logger.warning("NAV history unavailable for %s (%s): %s", isin, code, exc)
        return {}
    _SERIES[isin] = s
    return s


def nav_on_or_before(isin: str, d: date) -> Optional[tuple[date, Decimal]]:
    s = series(isin, d)
    for k in range(0, 8):
        day = d - timedelta(days=k)
        if day in s:
            return day, s[day]
    return None
