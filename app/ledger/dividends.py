"""
Dividends proven by arithmetic, from different stocks.

A company pays dividend per share to whoever held the shares before the ex-date, into the bank 2-5 weeks later, and the
bank narration is a truncated payer name ("OIL AND NATURA", "TATASTEELDIV 2026"). The public record of ex-dates and
per-share amounts (Yahoo Finance) plus the shares we held before each ex-date (the ledger's own trades) make a credit
provable: it is a dividend when  shares held before the ex-date x per-share amount  equals the credit to the paisa
(or 90% of it, after TDS; some registrars pay whole rupees), within 120 days after the ex-date. Yahoo's per-share amounts are
adjusted for LATER splits and bonus issues, so the amount actually paid is tried too (adjusted x the ratio of every later split).
Several candidates are separated by the narration. A credit whose narration names a stock, whose amount is a whole number of shares
x that stock's per-share amount, but for which our trades do not say how many shares were held (bought before the history) is
matched on the name and the arithmetic, and says so.

    refresh_history()   daily, with the other evidence: fetch ex-dates/amounts per traded stock into data/cache/dividends.json
    scan()              no network: match credits to (stock, ex-date); report what could not be matched and why
    tag()               matched credits become income (`dividend`) and flows of that stock in the one returns model
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional

import httpx
from sqlalchemy.orm import Session

from app.models.schemas import Account, InstrumentTrade, Transaction

logger = logging.getLogger(__name__)
D = Decimal
CACHE = Path("data/cache/dividends.json")
STALE_DAYS = 7
WINDOW = timedelta(days=120)              # a final dividend is credited after the AGM, up to ~3-4 months after the ex-date
_CANDIDATE = re.compile(r"^ACH ?CR|DIVIDEND|\bDIV\b|INT ?DIV|FNL ?DIV|FINAL ?DIV", re.I)
_SKIP = re.compile(r"CENTRAL DEPOSITO|CDSL|SOVEREIGN GOLD|INTEREST|INT TRF", re.I)       # coupons and interest are other evidence


def _base_symbol(s: str | None) -> str:
    return re.split(r"[- ]", (s or "").strip())[0].upper()


def symbols(db: Session) -> dict[str, str]:
    """{ISIN: NSE ticker} of every stock traded, from the contract notes (the series suffix -A, -B... is dropped)."""
    seen: dict[str, Counter] = defaultdict(Counter)
    for t in db.query(InstrumentTrade).filter(InstrumentTrade.holding_type == "equity", InstrumentTrade.isin.isnot(None)):
        b = _base_symbol(t.symbol or t.instrument_name)
        if b and b.isalnum():
            seen[t.isin][b] += 1
    return {i: c.most_common(1)[0][0] for i, c in seen.items()}


# tickers renamed since the contract notes were issued: the public history lives under the new one
RENAMED = {"ZOMATO": "ETERNAL", "TATAMOTORS": "TMPV"}


# ── public history ───────────────────────────────────────────────────────────
def fetch(symbol: str) -> tuple[list[tuple[date, Decimal]], list[tuple[date, Decimal, Decimal]]]:
    """(dividends [(ex-date, amount)], splits [(date, numerator, denominator)]); bonus issues appear as splits (1:3 bonus = 4:3)."""
    r = httpx.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{RENAMED.get(symbol, symbol)}.NS?range=max&interval=1mo&events=div,splits",
                  timeout=25, headers={"User-Agent": "Mozilla/5.0 (kutumbh dividends)"})
    r.raise_for_status()
    ev = r.json()["chart"]["result"][0].get("events", {})
    divs = sorted((datetime.fromtimestamp(int(v["date"])).date(), D(str(v["amount"]))) for v in ev.get("dividends", {}).values())
    splits = sorted((datetime.fromtimestamp(int(v["date"])).date(), D(str(v["numerator"])), D(str(v["denominator"])))
                    for v in ev.get("splits", {}).values())
    return divs, splits


def _load() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return {}


def history(symbol: str) -> list[tuple[date, Decimal]]:
    rows = _load().get(symbol, {}).get("rows", [])
    return [(date.fromisoformat(d), D(a)) for d, a in rows]


def splits(symbol: str) -> list[tuple[date, Decimal, Decimal]]:
    return [(date.fromisoformat(d), D(n), D(m)) for d, n, m in _load().get(symbol, {}).get("splits", [])]


def later_split_factor(sym_splits: list, ex: date) -> Decimal:
    """What one share at the ex-date became: the product of numerator/denominator of every split or bonus AFTER it."""
    f = D(1)
    for d, n, m in sym_splits:
        if d > ex and m:
            f *= n / m
    return f


def refresh_history(db: Session, fetcher: Callable = fetch, today: date | None = None) -> dict:
    """Fetch what is missing or older than a week. A failed fetch keeps the cached rows (and is counted), nothing is invented."""
    today = today or date.today()
    cache, out = _load(), {"fetched": 0, "failed": 0, "kept": 0}
    for sym in sorted(set(symbols(db).values())):
        at = cache.get(sym, {}).get("at")
        if at and "splits" in cache[sym] and (today - date.fromisoformat(at)).days < STALE_DAYS:
            out["kept"] += 1
            continue
        try:
            got = fetcher(sym)
            rows, sp = got if isinstance(got, tuple) else (got, [])
        except Exception as exc:
            logger.warning("dividend history for %s unavailable: %s", sym, exc)
            out["failed"] += 1
            continue
        cache[sym] = {"at": today.isoformat(), "rows": [[d.isoformat(), str(a)] for d, a in rows],
                      "splits": [[d.isoformat(), str(n), str(m)] for d, n, m in sp]}
        out["fetched"] += 1
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache))
    except Exception as exc:
        logger.warning("dividend cache not written: %s", exc)
    return out


# ── matching ─────────────────────────────────────────────────────────────────
def _units_before(trades: list[InstrumentTrade], owner: str, isin: str, day: date) -> Decimal:
    """Shares entitled to the dividend: bought before the ex-date and not sold before it."""
    n = D(0)
    for t in trades:
        if t.owner_id == owner and t.isin == isin and t.trade_date < day:
            n += D(t.quantity) if t.side == "B" else -D(t.quantity)
    return n


def _narration_names(db: Session, isins: set[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for t in db.query(InstrumentTrade).filter(InstrumentTrade.isin.in_(isins)):
        for word in re.findall(r"[A-Za-z]{4,}", f"{t.symbol or ''} {t.instrument_name or ''}"):
            out[t.isin].add(word.upper()[:6])
        if t.symbol:
            out[t.isin].add(_base_symbol(t.symbol)[:6])
    return out


def _per_share_candidates(adjusted: Decimal, factor: Decimal) -> list[Decimal]:
    out = [adjusted]
    paid = (adjusted * factor).quantize(D("0.0001"))
    if paid != adjusted:
        out.append(paid)                                           # Yahoo shows the amount per CURRENT share; the bank paid per OLD share
    return out


def _fits(units: Decimal, dps: Decimal, amount: Decimal) -> str | None:
    for kind, f in (("gross", D(1)), ("after_tds", D("0.9"))):
        want = units * dps * f
        if abs(want - amount) <= D("0.011"):
            return kind
        if amount == amount.to_integral_value() and D(0) <= want - amount < D(1):
            return kind + "_rounded_down"                          # some registrars pay whole rupees
    return None


def scan(db: Session, today: date | None = None) -> dict:
    today = today or date.today()
    syms = symbols(db)
    trades = db.query(InstrumentTrade).filter(InstrumentTrade.holding_type == "equity", InstrumentTrade.isin.isnot(None)).all()
    hist = {i: history(s) for i, s in syms.items()}
    sp = {i: splits(s) for i, s in syms.items()}
    names = _narration_names(db, set(syms))
    credits = (db.query(Transaction, Account.owner_id).join(Account, Account.id == Transaction.account_id)
               .filter(Account.account_type == "bank", Transaction.transaction_type == "credit").order_by(Transaction.date).all())
    matched, unmatched, ambiguous = [], [], []
    for t, owner in credits:
        narr = t.raw_description or t.description or ""
        if not _CANDIDATE.search(narr) or _SKIP.search(narr):
            continue
        amount, hits = D(t.amount), []
        flat = re.sub(r"[^A-Z]", "", narr.upper())
        for isin, rows in hist.items():
            for ex, adj in rows:
                if not (D(0) <= D((t.date - ex).days) <= D(WINDOW.days)):
                    continue
                factor = later_split_factor(sp[isin], ex)
                units = _units_before(trades, owner, isin, ex)
                for dps in _per_share_candidates(adj, factor):
                    if units > 0 and (kind := _fits(units, dps, amount)):
                        hits.append({"isin": isin, "symbol": syms[isin], "ex_date": ex, "dps": dps, "units": units, "kind": kind,
                                     "units_from": "trades"})
        if len(hits) > 1:                                         # the narration names the company
            named = [h for h in hits if any(w in flat for w in names.get(h["isin"], ()))]
            hits = named if len(named) == 1 else hits
        if not hits:                                              # the stock is named and the sum is a whole number of shares of it
            for isin, rows in hist.items():
                if not any(w in flat for w in names.get(isin, ())):
                    continue
                if any(_units_before(trades, owner, isin, ex) > 0 for ex, _ in rows):
                    continue                                      # our trades DO say what was held: the arithmetic must fit them
                for ex, adj in rows:
                    if not (D(0) <= D((t.date - ex).days) <= D(WINDOW.days)):
                        continue
                    for dps in _per_share_candidates(adj, later_split_factor(sp[isin], ex)):
                        n = amount / dps
                        if dps > 0 and abs(n - n.to_integral_value()) < D("0.0005") and n >= 1:
                            hits.append({"isin": isin, "symbol": syms[isin], "ex_date": ex, "dps": dps, "units": n.to_integral_value(),
                                         "kind": "gross", "units_from": "amount (shares held before our trade history)"})
            hits = hits if len(hits) == 1 else []
        row = {"owner": owner, "date": t.date.isoformat(), "amount": float(amount), "narration": narr[:60], "txn_id": str(t.id)}
        if len(hits) == 1:
            h = hits[0]
            matched.append({**row, "isin": h["isin"], "symbol": h["symbol"], "ex_date": h["ex_date"].isoformat(),
                            "dps": float(h["dps"]), "units": float(h["units"]), "kind": h["kind"], "units_from": h["units_from"]})
        elif hits:
            ambiguous.append({**row, "could_be": sorted({h["symbol"] for h in hits})})
        else:
            unmatched.append(row)
    return {"as_of": today.isoformat(), "matched": matched, "unmatched": unmatched, "ambiguous": ambiguous,
            "symbols_without_history": sorted(s for i, s in syms.items() if not hist.get(i))}


def tag(db: Session, today: date | None = None) -> int:
    """Matched credits become income (`dividend`) carrying the stock's ISIN, so the returns model counts them as flows of
    that stock. Rows you labelled yourself are left alone; no network (history comes from the cache)."""
    changed = 0
    for m in scan(db, today)["matched"]:
        t = db.get(Transaction, m["txn_id"])
        if t.category_source == "user":
            continue
        if (t.category, t.subcategory, t.isin, t.category_source) != ("income", "dividend", m["isin"], "trade"):
            t.category, t.subcategory, t.isin, t.category_source, t.needs_review = "income", "dividend", m["isin"], "trade", False
            changed += 1
    db.flush()
    return changed
