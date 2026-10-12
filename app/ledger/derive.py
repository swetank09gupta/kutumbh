"""
Units the depository shows but no contract note / Coin mail / tradebook explains.

A fund unit credit in the demat is hard evidence of *quantity and date*; its price is public
(AMFI NAV on the allotment day) and the money that paid for it is in the bank statement. So an
unexplained credit is turned into a trade — source 'derived' — instead of an unknown-cost lot:

    units, date   from the demat line / CDSL alert           (exact)
    price         NAV history on the allotment day            (public, exact to the paisa)
    cash          the matching bank debit when there is one   (confirms the NAV day)

Rules that keep it honest:
  * only fund ISINs, only lines no real trade matched, only when the lines add up to the
    statement's own change (interval residual) or the pre-history opening balance;
  * derived trades are deleted and re-derived on every refresh, so a real trade that arrives
    later (Coin mail, Console tradebook) simply replaces them — nothing is ever double counted;
  * they are marked source 'derived' everywhere, so a number resting on them can say so.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ledger import rules
from app.ledger.engine import _to_move, compute_owner, statement_balances
from app.ledger.nav import nav_on_or_before, series
from app.models.schemas import Account, DematMovement, InstrumentTrade, Transaction

logger = logging.getLogger(__name__)
SOURCE = "derived"
_BANK_HINT = ("zerodha", "iccl", "indian clearin", "coin", "bsestar", "nse clearing", "bse ")


def purge(db: Session) -> int:
    """Drop every derived trade AND every derived cashflow row (by source, not by link, so an
    orphan left by an interrupted run can never survive and be counted twice)."""
    db.query(Transaction).filter(Transaction.source_file == "derived:demat+nav").delete(synchronize_session=False)
    n = db.query(InstrumentTrade).filter(InstrumentTrade.source == SOURCE).delete(synchronize_session=False)
    db.flush()
    return n


def _dedupe_lines(rows: list[DematMovement]) -> list[rules.Movement]:
    """The same credit appears as a statement line and as a CDSL alert: count it once."""
    seen: dict[tuple, rules.Movement] = {}
    for r in sorted(rows, key=lambda r: ((r.description or "").startswith("CDSL alert"), r.move_date)):
        m = _to_move(r)
        seen.setdefault((m.isin, m.move_date, m.qty_in, m.qty_out), m)
    return list(seen.values())


def _prev_bdays(d: date, n: int) -> list[date]:
    out, x = [], d
    while len(out) < n:
        x -= timedelta(days=1)
        if x.weekday() < 5:
            out.append(x)
    return out


def _bank_flows(db: Session, owner_id: str) -> list[Transaction]:
    return (db.query(Transaction).join(Account, Account.id == Transaction.account_id)
            .filter(Account.owner_id == owner_id, Account.account_type == "bank", Transaction.amount > 0)
            .all())


def _price_it(isin: str, line: rules.Movement, bank: list[Transaction], used: set) -> Optional[dict]:
    """NAV day + cash for one unit credit/debit. Bank evidence picks the day when it can."""
    credit = line.qty_in > 0
    best = None
    for day in _prev_bdays(line.move_date, 5) + [line.move_date]:
        hit = nav_on_or_before(isin, day)
        if not hit or hit[0] != day:
            continue
        cash = line.qty * hit[1]
        for t in bank:
            if t.id in used or t.transaction_type != ("debit" if credit else "credit"):
                continue
            if not (day - timedelta(days=6) <= t.date <= day + timedelta(days=2)):
                continue
            if abs(Decimal(t.amount) - cash) <= cash * Decimal("0.006") + 1:
                # same amount twice (two monthly SIP debits of 10,000): the one nearest the NAV day is this line's
                key = (round(float(abs(Decimal(t.amount) - cash) / cash), 3), abs((t.date - day).days))
                if best is None or key < best[0]:
                    best = (key, day, hit[1], Decimal(t.amount), t)
    if best:
        _, day, nav, amt, t = best
        used.add(t.id)
        return {"date": day, "nav": nav, "amount": amt, "bank": t}
    hit = None
    for day in _prev_bdays(line.move_date, 2):
        hit = nav_on_or_before(isin, day)
        if hit:
            break
    if not hit:
        return None
    return {"date": hit[0], "nav": hit[1], "amount": (line.qty * hit[1]).quantize(Decimal("0.01")), "bank": None}


def _find_by_bank(isin: str, units: Decimal, lo: date, hi: date, bank: list[Transaction], used: set,
                  credit: bool) -> Optional[dict]:
    """Which bank debit paid for exactly `units` of this fund on some NAV day in the window?"""
    best = None
    for t in bank:
        if t.id in used or t.transaction_type != ("debit" if credit else "credit"):
            continue
        if not (lo <= t.date <= hi) or not any(h in (t.raw_description or "").lower() for h in _BANK_HINT):
            continue
        for k in range(-1, 6):
            day = t.date + timedelta(days=k)
            if day.weekday() >= 5:
                continue
            hit = nav_on_or_before(isin, day)
            if not hit or hit[0] != day:
                continue
            err = abs(Decimal(t.amount) - units * hit[1])
            if err <= Decimal(t.amount) * Decimal("0.006") + 1 and (best is None or err < best[0]):
                best = (err, day, hit[1], Decimal(t.amount), t)
    if not best:
        return None
    _, day, nav, amt, t = best
    used.add(t.id)
    return {"date": day, "nav": nav, "amount": amt, "bank": t}


def derive_for_owner(db: Session, owner_id: str) -> int:
    led = compute_owner(db, owner_id)
    dates, qty_by_date, _meta = statement_balances(db, owner_id)
    if not dates:
        return 0
    rows_by_isin: dict[str, list[DematMovement]] = defaultdict(list)
    for r in db.query(DematMovement).filter(DematMovement.owner_id == owner_id).all():
        rows_by_isin[r.isin].append(r)
    acct = (db.query(Account).filter(Account.owner_id == owner_id, Account.institution == "Zerodha Coin").first()
            or db.query(Account).filter(Account.owner_id == owner_id, Account.institution == "Zerodha",
                                        Account.account_type == "stocks").first())
    if acct is None:
        return 0
    bank, used, made = _bank_flows(db, owner_id), set(), 0

    def add(isin: str, name: str, side: str, qty: Decimal, settle: date, priced: dict) -> None:
        nonlocal made
        fp = hashlib.sha256(f"derived|{owner_id}|{isin}|{settle}|{qty}|{side}".encode()).hexdigest()
        price = priced["nav"]
        db.add(Transaction(
            account_id=acct.id, date=priced["date"], amount=priced["amount"],
            transaction_type="debit" if side == "B" else "credit",
            description=f"{'BUY' if side == 'B' else 'REDEEM'} {qty} units of {name} @ NAV {price} (derived)",
            raw_description=f"derived from demat evidence {settle}; NAV {priced['date']}"
                            + ("; cash confirmed by bank" if priced["bank"] else "; cash = units x NAV"),
            category="investment", counterparty=name[:200], isin=isin, source_file="derived:demat+nav",
            fingerprint=fp, category_source="trade"))
        db.add(InstrumentTrade(
            id=uuid.uuid4(), owner_id=owner_id, account_id=acct.id, isin=isin, symbol=name[:200],
            instrument_name=name[:200], holding_type="mutual_fund", trade_date=priced["date"],
            settle_date=settle, side=side, quantity=qty, price=price, amount=priced["amount"],
            source=SOURCE, source_ref=("bank:" + priced["bank"].fingerprint[:16]) if priced["bank"] else "nav",
            source_file="derived:demat+nav",
            natural_key=f"dv|{owner_id}|{isin}|{settle}|{qty}|{side}", txn_fingerprint=fp))
        made += 1

    line_jobs: list[tuple[str, str, rules.Movement]] = []              # unit credit/debit we know the date of
    rest_jobs: list[tuple[str, str, Decimal, date, date, bool]] = []   # units the statements show with no line at all

    for isin, st in sorted(led.states.items()):
        if not isin.startswith("INF"):
            continue
        tol = rules.tol_for(isin)
        trades = [t for t in led.trades if t.isin == isin]
        lines = _dedupe_lines(rows_by_isin.get(isin, []))
        orphans = rules.match_movements(trades, lines).unmatched_movements
        name = (led.meta.get(isin) or {}).get("name") or isin
        balances = [(d, qty_by_date[d].get(isin, rules.ZERO)) for d in dates]
        d0, q0 = balances[0]

        # Each place the statements say units appeared/left with no trade: (window, units to explain)
        gaps: list[tuple[date, date, Decimal]] = []
        opening = q0 - sum((t.signed for t in trades if rules.settle_date(t) <= d0), rules.ZERO)
        if abs(opening) > tol:
            gaps.append((d0 - timedelta(days=900), d0, opening))
        skip = rules.boundary_indices(st.intervals)
        for i, iv in enumerate(st.intervals):
            if not iv.reconciled and i not in skip:
                gaps.append((iv.start, iv.end, iv.residual))

        for lo, hi, need in gaps:
            inside = [m for m in orphans if (m.move_date <= hi and (m.move_date > lo or hi == d0))]
            part = sum((m.signed for m in inside), rules.ZERO)
            use = inside if (need > 0 and part > 0 and part <= need + tol) or (need < 0 and part < 0 and part >= need - tol) else []
            line_jobs += [(isin, name, m) for m in use]
            remainder = need - sum((m.signed for m in use), rules.ZERO)
            if abs(remainder) > tol:
                rest_jobs.append((isin, name, remainder, max(lo, hi - timedelta(days=45)) if hi != d0 else lo, hi, remainder > 0))
        # after the last statement: CDSL alerts that no pending trade accounts for
        line_jobs += [(isin, name, m) for m in st.alert_only]

    # Units with no line have only their amount as evidence (units x that day's NAV == a bank debit), so they
    # choose first; a line then takes the debit nearest its own date. (Otherwise a line could take the debit
    # that is the only possible match for an earlier purchase.)
    for isin, name, remainder, lo, hi, is_buy in rest_jobs:
        units = abs(remainder).quantize(Decimal("0.001"))
        found = _find_by_bank(isin, units, lo, hi, bank, used, is_buy)
        if found:
            add(isin, name, "B" if is_buy else "S", units, found["date"], found)
    for isin, name, line in sorted(line_jobs, key=lambda j: j[2].move_date):
        priced = _price_it(isin, line, bank, used)
        if priced:
            add(isin, name, "B" if line.qty_in > 0 else "S", line.qty, line.move_date, priced)
    db.flush()
    return made


def derive_all(db: Session) -> dict:
    """Delete last run's derived trades, derive again from today's evidence."""
    from app.ledger.engine import canonicalize_isins, resolve_isins
    removed = purge(db)
    canonicalize_isins(db)
    resolve_isins(db)
    db.flush()
    owners = sorted({o for (o,) in db.query(DematMovement.owner_id).distinct().all()})
    made = {}
    for o in owners:
        try:
            made[o] = derive_for_owner(db, o)
        except Exception as exc:                         # one owner's failure must not stop the others
            logger.error("derive failed for %s: %s", o, exc, exc_info=True)
            db.rollback()
            made[o] = -1
    db.commit()
    return {"removed": removed, "derived": made}
