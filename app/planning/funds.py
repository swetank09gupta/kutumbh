"""
What a holding really is, from market data (docs/PLANNING.md §6), not from its name.

  category   the SEBI category AMFI lists the scheme under (NAVAll.txt, daily)
  contents   how the fund actually behaves: its own monthly returns regressed on real index series (Nifty 50, Nifty Midcap 150, Nifty Smallcap 250, gold,
             gilts) with non-negative weights summing to 1 (return-based style analysis); when there is too little history, the SEBI category's mandated
             composition. A direct stock is its beta to the market and the volatility the market does not explain.
  risk       measured from those same returns (`risk.py` turns it into the years a holding needs)

Holdings of a fund (the shares in it) are published by each AMC as a monthly spreadsheet, with no common machine-readable source; the style regression is
what the fund's price shows it holding. Everything is cached (a week for prices, a day for the catalogue) and degrades to the SEBI category, then to the name.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

FACTORS = ("large", "mid", "small", "gold", "debt")
MIN_MONTHS = 24                    # fewer overlapping months than this: the category's own composition is used
MAX_MONTHS = 60                    # the regression looks at the last five years: what the fund holds now
MIN_R2 = 0.6                       # a style fit explaining less than this of the fund's movements is not trusted
CATALOG_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
MFAPI = "https://api.mfapi.in/mf/{code}"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=max&interval=1mo"
UA = {"User-Agent": "Mozilla/5.0"}
IST = timezone(timedelta(hours=5, minutes=30))
TTL_PRICES, TTL_CATALOG = 7, 1     # days
YAHOO_FACTORS = {"mid": "MID150BEES.NS", "small": "NIFTYSMLCAP250.NS"}


def _cache_dir() -> Path:
    p = Path(os.getenv("FUNDS_CACHE", "data/cache/funds"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _fresh(path: Path, days: float) -> bool:
    return path.exists() and (datetime.now().timestamp() - path.stat().st_mtime) < days * 86400


def _cached_json(name: str, ttl: float, fetch):
    """JSON from the cache while fresh, else fetched; on a fetch failure the stale copy, else None."""
    path = _cache_dir() / name
    if _fresh(path, ttl):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            pass
    try:
        data = fetch()
        path.write_text(json.dumps(data))
        return data
    except Exception as exc:
        logger.warning("%s: fetch failed (%s)", name, exc)
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (OSError, ValueError):
                return None
        return None


# ── catalogue: SEBI category per scheme ────────────────────────────────────────────────────────────────────────────────────────────────────


def parse_catalog(text: str) -> dict[str, dict]:
    """AMFI NAVAll.txt → {isin: {code, name, category}} (both the growth/payout and the reinvestment ISIN of every scheme)."""
    out: dict[str, dict] = {}
    category = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(Open Ended|Close Ended|Interval Fund)[^(]*\((.+)\)$", line, re.I)
        if m:
            category = m.group(2).strip()
            continue
        parts = line.split(";")
        if len(parts) >= 6 and parts[0].isdigit():
            rec = {"code": int(parts[0]), "name": parts[3].strip() if len(parts) > 3 else "", "category": category}
            for isin in (parts[1].strip(), parts[2].strip()):
                if re.match(r"^IN[A-Z0-9]{10}$", isin):
                    out[isin] = rec
    return out


def catalog() -> dict[str, dict]:
    def fetch():
        import httpx
        r = httpx.get(CATALOG_URL, timeout=60, headers=UA, follow_redirects=True)
        r.raise_for_status()
        cat = parse_catalog(r.text)
        if len(cat) < 1000:
            raise ValueError("catalogue looks incomplete")
        return cat
    return _cached_json("amfi_catalog.json", TTL_CATALOG, fetch) or {}


# ── prices ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────


def _monthly(prices: dict[str, float], today: date | None = None) -> pd.Series:
    from app.planning.history import month_end_returns
    return month_end_returns(prices, today)


def fund_returns(code: int, today: date | None = None) -> pd.Series | None:
    """Monthly returns of an AMFI scheme (mfapi.in NAVs, net of the fund's costs)."""
    from app.planning import history
    navs = _cached_json(f"nav_{code}.json", TTL_PRICES, lambda: history.fetch_nav(code))
    return _monthly(navs, today) if navs else None


def yahoo_returns(symbol: str, today: date | None = None) -> pd.Series | None:
    def fetch():
        import httpx
        r = httpx.get(YAHOO.format(sym=symbol), timeout=30, headers=UA).json()["chart"]["result"][0]
        closes = (r["indicators"].get("adjclose") or [{}])[0].get("adjclose") or r["indicators"]["quote"][0]["close"]
        # a bar is stamped 00:00 India time on its first day (18:30 UTC the day before): read the date in India, or every month shifts back by one
        return {datetime.fromtimestamp(t, tz=IST).date().isoformat(): float(c) for t, c in zip(r["timestamp"], closes) if c}
    px = _cached_json(f"yahoo2_{re.sub(r'[^A-Za-z0-9]', '_', symbol)}.json", TTL_PRICES, fetch)
    return _monthly(px, today) if px else None


def stock_symbol(isin: str) -> dict | None:
    """{symbol, sector, industry} for an Indian share, found from its ISIN (Yahoo search), cached 90 days; None if unknown."""
    def fetch():
        import httpx
        q = httpx.get("https://query1.finance.yahoo.com/v1/finance/search", params={"q": isin, "quotesCount": 5, "newsCount": 0}, timeout=30, headers=UA).json()["quotes"]
        q = [x for x in q if x.get("quoteType") == "EQUITY"]
        if not q:
            raise ValueError("not found")
        x = next((x for x in q if str(x.get("symbol", "")).endswith(".NS")), q[0])
        return {"symbol": x["symbol"], "sector": x.get("sector"), "industry": x.get("industry")}
    return _cached_json(f"symbol_{isin}.json", 90, fetch)


def factor_frame(data: dict, today: date | None = None) -> pd.DataFrame | None:
    """Monthly returns of the five factors over the months they all share: Nifty 50 / gold / gilts from the planning history, Midcap 150 and
    Smallcap 250 from Yahoo. None when a series cannot be had (no network and no cache)."""
    cols = {"large": data["joint"]["equity"], "gold": data["joint"]["gold"], "debt": data["joint"]["debt"]}
    for f, sym in YAHOO_FACTORS.items():
        s = yahoo_returns(sym, today)
        if s is None or len(s) < 36:
            return None
        cols[f] = s
    df = pd.DataFrame(cols).dropna()
    return df[list(FACTORS)] if len(df) >= 36 else None


# ── contents ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────


def simplex_ls(y: np.ndarray, X: np.ndarray, prior: np.ndarray | None = None, strength: float = 0.5) -> np.ndarray:
    """argmin |y - Xw|² (+ a pull toward `prior`) with w >= 0 and sum(w) = 1, exactly: solve each subset of factors under the sum constraint and keep
    the best feasible one. The Nifty indices move together (mid and small caps correlate ~0.9 with each other), so a fund's own history alone cannot tell
    them apart reliably; the pull toward the composition its SEBI category requires settles that, worth `strength` months of data per unit of weight."""
    n, k = X.shape
    if prior is not None:
        c = float(np.sqrt(strength * n) * X.std(axis=0).mean())
        X, y = np.vstack([X, c * np.eye(k)]), np.concatenate([y, c * prior])
    best_w, best_sse = None, np.inf
    for r in range(1, k + 1):
        for idx in itertools.combinations(range(k), r):
            Xs = X[:, idx]
            A = np.zeros((r + 1, r + 1))                     # KKT system of: minimise |y - Xs w|² subject to 1'w = 1
            A[:r, :r] = 2 * Xs.T @ Xs
            A[:r, r] = A[r, :r] = 1.0
            b = np.concatenate([2 * Xs.T @ y, [1.0]])
            try:
                sol = np.linalg.solve(A, b)[:r]
            except np.linalg.LinAlgError:
                continue
            if (sol < -1e-9).any():
                continue
            sse = float(((y - Xs @ sol) ** 2).sum())
            if sse < best_sse - 1e-15:
                w = np.zeros(k)
                w[list(idx)] = np.clip(sol, 0, None)
                best_w, best_sse = w / w.sum(), sse
    return best_w


# What SEBI's categorisation circular (Oct 2017, as amended) says each category must hold; used only when the fund's own history is too short or
# does not fit. Ranges are taken at their middle. https://www.sebi.gov.in/legal/circulars/oct-2017/categorization-and-rationalization-of-mutual-fund-schemes_36199.html
_E = lambda large=0.0, mid=0.0, small=0.0, gold=0.0, debt=0.0: np.array([large, mid, small, gold, debt])
SEBI_PRIOR = [
    (r"large (&|and) mid", _E(0.50, 0.50)),
    (r"small cap", _E(0.20, 0.15, 0.65)),
    (r"mid cap|midcap", _E(0.20, 0.65, 0.15)),
    (r"large cap", _E(0.85, 0.15)),
    (r"flexi|multi cap|focused|value|contra|dividend yield|elss|sectoral|thematic", _E(0.55, 0.25, 0.20)),
    (r"aggressive hybrid|balanced hybrid|dynamic asset|balanced advantage|multi asset", _E(0.45, 0.15, 0.05, 0.05, 0.30)),
    (r"equity savings|arbitrage|conservative hybrid", _E(0.15, 0, 0, 0, 0.85)),
    (r"gold|silver", _E(gold=1.0)),
    (r"liquid|overnight|money market|ultra short|low duration|short duration|medium|long duration|dynamic bond|corporate bond|banking|credit risk|gilt|floater|debt|income|fmp|target maturity", _E(debt=1.0)),
]


def category_prior(category: str | None, name: str | None = None) -> np.ndarray | None:
    """Composition SEBI requires of the category; index funds from the index they track (named in the scheme)."""
    text = f"{category or ''} {name or ''}".lower()
    if "index" in text or "etf" in text:
        for pat, w in ((r"smallcap|small cap", _E(0.2, 0.15, 0.65)), (r"midcap|mid cap", _E(0.2, 0.65, 0.15)), (r"next 50|junior", _E(0.5, 0.5)),
                       (r"gold|silver", _E(gold=1.0)), (r"gilt|g-sec|bond|liquid|debt|sdl|treasury", _E(debt=1.0)), (r"nifty|sensex|bse|index", _E(large=1.0))):
            if re.search(pat, text):
                return w
    for pat, w in SEBI_PRIOR:
        if re.search(pat, text):
            return w
    return None


@dataclass
class Profile:
    key: str
    exposure: np.ndarray            # per factor (FACTORS order): sums to 1 for a fund, a beta for a stock
    idio: float                     # annual variance the factors do not explain
    source: str                     # style | sebi_category | beta | name | default
    category: str | None = None
    r2: float | None = None
    months: int = 0
    note: str = ""

    def summary(self) -> dict:
        return {"source": self.source, "category": self.category, "r2": None if self.r2 is None else round(self.r2, 2), "months": self.months,
                "exposure": {f: round(float(x), 2) for f, x in zip(FACTORS, self.exposure) if abs(x) > 0.005}, "idio_vol": round(float(np.sqrt(self.idio)), 3)}


DEFAULT_IDIO = 0.04 ** 2            # a fund we know only by category: tracking error of 4% a year (judgement, flagged by source)


def fund_profile(key: str, returns: pd.Series | None, factors: pd.DataFrame, category: str | None, name: str | None = None) -> Profile:
    prior = category_prior(category, name)
    if returns is not None:
        df = pd.concat([returns.rename("y"), factors], axis=1, join="inner").dropna().tail(MAX_MONTHS)
        if len(df) >= MIN_MONTHS:
            y, X = df["y"].to_numpy(), df[list(FACTORS)].to_numpy()
            w = simplex_ls(y, X, prior)
            if w is not None:
                res = y - X @ w
                r2 = 1 - float((res ** 2).sum()) / max(float(((y - y.mean()) ** 2).sum()), 1e-12)
                if r2 >= MIN_R2:
                    return Profile(key, w, float(res.var(ddof=0) * 12), "style", category, r2, len(df))
    if prior is not None:
        return Profile(key, prior, DEFAULT_IDIO, "sebi_category", category, None, 0, "history too short or the style fit is poor")
    return Profile(key, _E(large=1.0), DEFAULT_IDIO * 4, "default", category, None, 0, "category unknown: treated as equity")


def stock_profile(key: str, returns: pd.Series | None, factors: pd.DataFrame, sector: str | None = None) -> Profile:
    """A direct stock: beta to the market (Nifty 50) and the volatility the market does not explain. Both come from its own prices."""
    if returns is not None:
        df = pd.concat([returns.rename("y"), factors["large"]], axis=1, join="inner").dropna().tail(MAX_MONTHS * 2)
        if len(df) >= 36:
            x, y = df["large"].to_numpy(), df["y"].to_numpy()
            beta = float(np.cov(x, y, ddof=0)[0, 1] / max(x.var(), 1e-12))
            resid = y - (y.mean() - beta * x.mean()) - beta * x
            return Profile(key, _E(large=beta), float(resid.var(ddof=0) * 12), "beta", f"Equity share ({sector})" if sector else "Equity share", None, len(df))
    return Profile(key, _E(large=1.0), 0.30 ** 2, "default", "Equity share", None, 0, "no price history: market beta 1 and 30% own volatility assumed")


def fixed_profile(key: str, kind: str) -> Profile:
    """FDs, PPF and government bonds: debt with no volatility of their own; SGBs and gold: gold; NPS: half market, half debt."""
    w = {"gold": _E(gold=1.0), "nps": _E(large=0.5, debt=0.5)}.get(kind, _E(debt=1.0))
    return Profile(key, w, 0.0 if kind != "gold" else 0.0, "fixed", kind)
