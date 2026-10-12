"""
PPF interest: the Government of India notifies the rate (quarterly) and the National Savings Institute
publishes the full history since 1968. We read that table instead of hard-coding 7.1%, and apply PPF's
own accounting rule to a contribution stream:

  * interest for a month = rate / 12 x the LOWEST balance between the 5th and the month end
    (a deposit after the 5th earns from the next month);
  * it is credited once a year, on 31 March, and compounds from then on.

Source: https://www.nsiindia.gov.in/InternalPage.aspx?Id_Pk=178 (cached a week; last known table offline).
"""
from __future__ import annotations

import calendar
import json
import logging
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

import ssl
import time

import certifi
import httpx

logger = logging.getLogger(__name__)

URL = "https://www.nsiindia.gov.in/InternalPage.aspx?Id_Pk=178"
_CACHE = Path("data/cache/ppf_rates.json")
_CACHE_DAYS = 7
# Last known table (used only when the page has never been reachable).
_FALLBACK = [("2016-04-01", 8.1), ("2016-10-01", 8.0), ("2017-04-01", 7.9), ("2017-07-01", 7.8),
             ("2018-01-01", 7.6), ("2018-10-01", 8.0), ("2019-07-01", 7.9), ("2020-04-01", 7.1)]

# nsiindia.gov.in serves only its own certificate (Let's Encrypt "YE2") without the intermediate, so a
# strict client cannot build the chain. These are the public intermediate + cross-signed root, nothing secret.
_CHAIN = Path(__file__).resolve().parents[2] / "config" / "certs" / "nsi-chain.pem"
_MEMO: dict = {"at": 0.0, "rows": None}

_ROW = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s+TO\s+(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}(?:\.\d+)?)", re.I)


def _d(day: str, month: str, year: str) -> date:
    y, m = int(year), int(month)
    return date(y, m, min(int(day), calendar.monthrange(y, m)[1]))      # the page prints "31.06.2019"


def parse_rates(text: str) -> list[tuple[str, float]]:
    """[(effective-from ISO date, rate %)], oldest first, from the NSI table text."""
    rows = {}
    for m in _ROW.finditer(re.sub(r"\s+", " ", text)):
        start = _d(m[1], m[2], m[3])
        rows[start.isoformat()] = float(m[7])
    return sorted(rows.items())


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    if _CHAIN.exists():
        ctx.load_verify_locations(str(_CHAIN))
    return ctx


def _load() -> list[tuple[str, float]]:
    if _MEMO["rows"] is not None and time.time() - _MEMO["at"] < 3600:
        return _MEMO["rows"]
    rows = _load_uncached()
    _MEMO.update(at=time.time(), rows=rows)
    return rows


def _load_uncached() -> list[tuple[str, float]]:
    try:
        c = json.loads(_CACHE.read_text())
        age = datetime.now() - datetime.fromisoformat(c["fetched_at"])
        if age < timedelta(days=_CACHE_DAYS) and c.get("rows"):
            return [tuple(r) for r in c["rows"]]
        stale = [tuple(r) for r in c.get("rows", [])]
    except Exception:
        stale = []
    try:
        html = httpx.get(URL, timeout=20, follow_redirects=True, verify=_ssl_context(),
                         headers={"User-Agent": "Mozilla/5.0"}).text
        text = re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style).*?</\1>", " ", html, flags=re.S | re.I))
        rows = parse_rates(text)
        if rows:
            _CACHE.parent.mkdir(parents=True, exist_ok=True)
            _CACHE.write_text(json.dumps({"fetched_at": datetime.now().isoformat(), "rows": rows}))
            return rows
    except Exception as exc:
        logger.warning("PPF rate table unavailable (%s) — using the last known one", exc)
    return stale or list(_FALLBACK)


def ppf_rate_on(day: date) -> float:
    rate = 0.0
    for start, r in _load():
        if date.fromisoformat(start) <= day:
            rate = r
    return rate or float(_FALLBACK[-1][1])


def ppf_current_rate(today: Optional[date] = None) -> float:
    return ppf_rate_on(today or date.today())


def ppf_balance(contribs: list[tuple[date, Decimal]], today: date, rate_on=ppf_rate_on) -> tuple[Decimal, Decimal]:
    """
    (balance credited so far, interest accrued since the last 31 March) for dated contributions,
    by the PPF rule. Accrual counts completed months only.
    """
    if not contribs:
        return Decimal(0), Decimal(0)
    contribs = sorted(contribs)
    first = contribs[0][0]
    y, m = first.year, first.month
    balance = Decimal(0)
    accrued = Decimal(0)
    while (y, m) < (today.year, today.month):              # completed months only
        last_day = calendar.monthrange(y, m)[1]
        fifth, end = date(y, m, 5), date(y, m, last_day)
        low = balance + sum((a for d, a in contribs if d <= fifth and d >= date(y, m, 1)), Decimal(0))
        # deposits up to the 5th count; deposits after the 5th do not raise this month's minimum
        accrued += low * Decimal(str(rate_on(date(y, m, 1)))) / Decimal(1200)
        balance += sum((a for d, a in contribs if date(y, m, 1) <= d <= end), Decimal(0))
        if m == 3:                                         # credited on 31 March
            balance += accrued.quantize(Decimal("0.01"))
            accrued = Decimal(0)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    balance += sum((a for d, a in contribs if d >= date(today.year, today.month, 1)), Decimal(0))
    return balance.quantize(Decimal("0.01")), accrued.quantize(Decimal("0.01"))
