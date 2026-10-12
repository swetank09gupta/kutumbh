"""
Dividends proven by arithmetic, from different stocks.

A company pays dividend per share to whoever held the shares before the ex-date, into the bank 2-5 weeks later, and the
bank narration is a truncated payer name ("OIL AND NATURA", "TATASTEELDIV 2026"). The public record of ex-dates and
per-share amounts (Yahoo Finance) plus the shares we held before each ex-date (the ledger's own trades) make a credit
provable: it is a dividend when  shares held before the ex-date x per-share amount  equals the credit to the paisa
(or 90% of it, after TDS), within 75 days after the ex-date. Several candidates are separated by the narration.

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
WINDOW = timedelta(days=75)               # a dividend is credited within weeks of the ex-date
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


# ── public history ───────────────────────────────────────────────────────────
def fetch(symbol: str) -> list[tuple[date, Decimal]]:
    r = httpx.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.NS?range=max&interval=1mo&events=div",
                  timeout=25, headers={"User-Agent": "Mozilla/5.0 (kutumbh dividends)"})
    r.raise_for_status()
    ev = r.json()["chart"]["result"][0].get("events", {}).get("dividends", {})
    return sorted((datetime.fromtimestamp(int(v["date"])).date(), D(str(v["amount"]))) for v in ev.values())


def _load() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return {}


def history(symbol: str) -> list[tuple[date, Decimal]]:
    rows = _load().get(symbol, {}).get("rows", [])
    return [(date.fromisoformat(d), D(a)) for d, a in rows]


def refresh_history(db: Session, fetcher: Callable = fetch, today: date | None = None) -> dict:
    """Fetch what is missing or older than a week. A failed fetch keeps the cached rows (and is counted), nothing is invented."""
    today = today or date.today()
    cache, out = _load(), {"fetched": 0, "failed": 0, "kept": 0}
    for sym in sorted(set(symbols(db).values())):
        at = cache.get(sym, {}).get("at")
        if at and (today - date.fromisoformat(at)).days < STALE_DAYS:
            out["kept"] += 1
            continue
        try:
            rows = fetcher(sym)
        except Exception as exc:
            logger.warning("dividend history for %s unavailable: %s", sym, exc)
            out["failed"] += 1
            continue
        cache[sym] = {"at": today.isoformat(), "rows": [[d.isoformat(), str(a)] for d, a in rows]}
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


def scan(db: Session, today: date | None = None) -> dict:
    today = today or date.today()
    syms = symbols(db)
    trades = db.query(InstrumentTrade).filter(InstrumentTrade.holding_type == "equity", InstrumentTrade.isin.isnot(None)).all()
    hist = {i: history(s) for i, s in syms.items()}
    names = _narration_names(db, set(syms))
    credits = (db.query(Transaction, Account.owner_id).join(Account, Account.id == Transaction.account_id)
               .filter(Account.account_type == "bank", Transaction.transaction_type == "credit").order_by(Transaction.date).all())
    matched, unmatched, ambiguous = [], [], []
    for t, owner in credits:
        narr = t.raw_description or t.description or ""
        if not _CANDIDATE.search(narr) or _SKIP.search(narr):
            continue
        amount, hits = D(t.amount), []
        for isin, rows in hist.items():
            for ex, dps in rows:
                if not (D(0) <= D((t.date - ex).days) <= D(WINDOW.days)):
                    continue
                units = _units_before(trades, owner, isin, ex)
                if units <= 0:
                    continue
                for kind, f in (("gross", D(1)), ("after_tds", D("0.9"))):
                    if abs(units * dps * f - amount) <= D("0.011"):
                        hits.append({"isin": isin, "symbol": syms[isin], "ex_date": ex, "dps": dps, "units": units, "kind": kind})
        if len(hits) > 1:                                         # the narration names the company
            flat = re.sub(r"[^A-Z]", "", narr.upper())
            named = [h for h in hits if any(w in flat for w in names.get(h["isin"], ()))]
            hits = named if len(named) == 1 else hits
        row = {"owner": owner, "date": t.date.isoformat(), "amount": float(amount), "narration": narr[:60], "txn_id": str(t.id)}
        if len(hits) == 1:
            h = hits[0]
            matched.append({**row, "isin": h["isin"], "symbol": h["symbol"], "ex_date": h["ex_date"].isoformat(),
                            "dps": float(h["dps"]), "units": float(h["units"]), "kind": h["kind"]})
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
