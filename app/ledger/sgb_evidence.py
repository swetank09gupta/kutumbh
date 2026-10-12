"""
What the bank statements prove about Sovereign Gold Bonds, found by arithmetic, never assumed.

A coupon is units x nominal price x 1.25% paid every six months from the issue date; a subscription debit is
units x nominal (or x online price, Rs.50 a gram less). So a bank credit that equals that sum for a known tranche,
within a few days of a coupon date, is that tranche's coupon and tells the units; a debit near an issue date that
equals units x price is the subscription and tells what was actually paid.

    scan()        coupon streams per (owner, tranche, units), the coupons due and missing, bonds the coupons reveal
                  that no statement lists
    paid_terms()  per ISIN, what the subscription debit shows was paid (the ledger uses it for cost and date)
    tag()         ISIN-tag the evidence so the one returns model counts it (coupons as income, the subscription as cost)
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.engine.loan_schedule import add_months
from app.ledger.sgb import COUPON_PCT_PER_HALF_YEAR, Tranche, tranche, tranches
from app.models.schemas import Account, Transaction

D = Decimal
DATE_TOL = timedelta(days=6)          # weekends and bank holidays push a coupon a few days
GRACE = timedelta(days=10)            # a coupon is "missing" only this long after its date
CENT = D("0.02")
_COUPON_HINT = re.compile(r"CENTRAL DEPOSITO|CDSL|SOVEREIGN GOLD|\bSGB", re.I)      # banks truncate the narration
_SUBSCRIPTION = re.compile(r"\bSGB|SOVEREIGN GOLD", re.I)
_GMS = re.compile(r"(\d+)\s*GMS?\b", re.I)


@dataclass
class Credit:
    txn: Transaction
    owner: str


def coupon_dates(t: Tranche, upto: date) -> list[date]:
    out, k = [], 1
    while True:
        d = add_months(t.issue_date, 6 * k)
        if d > min(upto, t.maturity_date):
            return out
        out.append(d)
        k += 1


def _near(d: date, dates: list[date]) -> Optional[date]:
    hit = [x for x in dates if abs(x - d) <= DATE_TOL]
    return min(hit, key=lambda x: abs(x - d)) if hit else None


def depository_units(db: Session, owner_ids: list[str]) -> dict[tuple[str, str], Decimal]:
    """{(owner, isin): units} for tranche ISINs on each owner's newest depository statement (the ledger's own input,
    so nothing here depends on the holdings the ledger produces). A bond absent from the newest statement is not held."""
    from sqlalchemy import func
    from app.models.schemas import StatementBalance as SB
    newest = {(o, f): d for o, f, d in db.query(SB.owner_id, SB.family, func.max(SB.as_on)).filter(SB.owner_id.in_(owner_ids))
              .group_by(SB.owner_id, SB.family)}
    out: dict[tuple[str, str], Decimal] = {}
    for r in db.query(SB).filter(SB.owner_id.in_(owner_ids), SB.isin.in_(list(tranches()))):
        if r.as_on == newest.get((r.owner_id, r.family)) and D(r.qty) > 0:
            out[(r.owner_id, r.isin)] = max(out.get((r.owner_id, r.isin), D(0)), D(r.qty))
    return out


def manual_units(db: Session, owner_ids: list[str]) -> dict[tuple[str, str], Decimal]:
    """{(owner, isin): grams} for SGBs typed into Manual Assets whose name or notes give the ISIN or the series
    (for example "SGB 2022-23 Series I"): a bond held outside the demat the statements cover."""
    from app.models.schemas import ManualAsset
    out: dict[tuple[str, str], Decimal] = {}
    for a in db.query(ManualAsset).filter(ManualAsset.asset_type == "sgb", ManualAsset.owner_id.in_(owner_ids)):
        text = f"{a.name or ''} {a.notes or ''}"
        for t in tranches().values():
            if t.isin in text or re.search(re.escape(t.series) + r"(?![IVX])", text, re.I):
                out[(a.owner_id, t.isin)] = out.get((a.owner_id, t.isin), D(0)) + D(str(a.quantity or 0))
    return out


def _bank_rows(db: Session, owner_ids: list[str]) -> list[tuple[Transaction, str]]:
    return (db.query(Transaction, Account.owner_id).join(Account, Account.id == Transaction.account_id)
            .filter(Account.account_type == "bank", Account.owner_id.in_(owner_ids)).all())


def _match_coupon(amount: Decimal, on: date, today: date) -> list[tuple[Tranche, int, date]]:
    """Every (tranche, whole units, coupon date) this credit could be: the sum fits to the paisa and the date fits the calendar."""
    out = []
    for t in tranches().values():
        per_unit = t.nominal * COUPON_PCT_PER_HALF_YEAR / 100
        base = int(amount / per_unit)
        for units in (base, base + 1):
            if units >= 1 and abs(t.coupon(D(units)) - amount) <= CENT:
                due = _near(on, coupon_dates(t, today + DATE_TOL))
                if due:
                    out.append((t, units, due))
    return out


def scan(db: Session, owner_ids: list[str], today: date | None = None) -> dict:
    today = today or date.today()
    rows = _bank_rows(db, owner_ids)
    depository = depository_units(db, owner_ids)
    held = {**manual_units(db, owner_ids), **depository}              # the depository is the authority where both exist

    streams: dict[tuple[str, str, int], list[dict]] = defaultdict(list)
    ambiguous = []
    for t, owner in rows:
        if t.transaction_type != "credit" or not _COUPON_HINT.search(t.raw_description or t.description or ""):
            continue
        fits = _match_coupon(D(t.amount), t.date, today)
        if len(fits) > 1:                                          # prefer a bond the owner is known to hold
            known = [f for f in fits if held.get((owner, f[0].isin)) == D(f[1])]
            fits = known if len(known) == 1 else fits
        if len(fits) == 1:
            tr, units, due = fits[0]
            streams[(owner, tr.isin, units)].append({"date": t.date, "due": due, "amount": D(t.amount), "txn": t})
        elif fits:
            ambiguous.append({"owner": owner, "date": t.date.isoformat(), "amount": float(t.amount)})

    # statement coverage: a coupon can only be called missing where the owner's bank data reaches past it
    last_seen: dict[str, date] = {}
    for t, owner in rows:
        last_seen[owner] = max(last_seen.get(owner, t.date), t.date)

    out_streams, found_not_held = [], []
    keys = set(streams) | {(o, i, int(u)) for (o, i), u in held.items() if u == int(u)}
    for owner, isin, units in sorted(keys):
        tr = tranche(isin)
        coupons = sorted(streams.get((owner, isin, units), []), key=lambda c: c["date"])
        holding = held.get((owner, isin))
        is_held = holding is not None and holding == D(units)
        due = coupon_dates(tr, today)
        got = {c["due"] for c in coupons}
        last = max(got) if got else None
        missing = [d for d in due if last and d > last and d + GRACE <= today and last_seen.get(owner, date.min) >= d + DATE_TOL]
        before_first = [d for d in due if got and d < min(got)]
        nxt = next((d for d in coupon_dates(tr, tr.maturity_date) if d > today), None)
        row = {"owner": owner, "isin": isin, "series": tr.series, "units": units, "held": is_held,
               "holding_units": float(holding) if holding is not None else None,
               "nominal": float(tr.nominal), "online_price": float(tr.online),
               "coupon_each": float(tr.coupon(D(units))),
               "coupons": [{"date": c["date"].isoformat(), "due": c["due"].isoformat(), "amount": float(c["amount"]),
                            "txn_id": str(c["txn"].id)} for c in coupons],
               "received_total": float(sum((c["amount"] for c in coupons), D(0))),
               "missing_after_last": [d.isoformat() for d in missing],
               "not_seen_before_first": [d.isoformat() for d in before_first],
               "next_coupon": nxt.isoformat() if nxt else None,
               "next_coupon_amount": float(tr.coupon(D(units))) if nxt else None}
        out_streams.append(row)
        if coupons and not is_held:
            found_not_held.append(row)
    return {"as_of": today.isoformat(), "streams": out_streams, "found_not_held": found_not_held, "ambiguous": ambiguous,
            "held": [{"owner": o, "isin": i, "units": float(u)} for (o, i), u in sorted(depository.items())],
            "held_by_hand": [{"owner": o, "isin": i, "units": float(u)} for (o, i), u in sorted(held.items()) if (o, i) not in depository]}


def subscription(db: Session, owner_id: str, t: Tranche, units: Decimal, rows=None) -> Optional[dict]:
    """The bank debit that paid for this subscription: near the issue date, SGB in the narration, units x nominal or x online."""
    rows = rows if rows is not None else _bank_rows(db, [owner_id])
    lo, hi = t.issue_date - timedelta(days=14), t.issue_date + timedelta(days=3)
    for tx, owner in rows:
        if owner != owner_id or tx.transaction_type != "debit" or not (lo <= tx.date <= hi):
            continue
        if not _SUBSCRIPTION.search(tx.raw_description or tx.description or ""):
            continue
        n = _GMS.search(tx.raw_description or "")
        for kind, price in (("online", t.online), ("nominal", t.nominal)):
            for u in ([D(n.group(1))] if n else [units]):
                if u == units and abs(D(tx.amount) - u * price) <= D(1):
                    return {"txn": tx, "price": price, "kind": kind, "units": u, "date": tx.date}
    return None


def paid_terms(db: Session, owner_id: str) -> dict[str, tuple[date, Decimal, Decimal]]:
    """{isin: (payment date, price per unit, units)} for subscriptions the bank statement proves."""
    out = {}
    rows = _bank_rows(db, [owner_id])
    for (_o, isin), units in depository_units(db, [owner_id]).items():
        hit = subscription(db, owner_id, tranche(isin), units, rows)
        if hit:
            out[isin] = (hit["date"], hit["price"], hit["units"])
    return out


def tag(db: Session, today: date | None = None) -> dict[str, int]:
    """Make the evidence count: coupons become income (ISIN-tagged only when the bond is held, so a bond no statement
    lists is never half-counted in returns); the subscription debit carries its ISIN. Rows you labelled yourself are left alone."""
    from app.models.schemas import Owner
    today = today or date.today()
    owners = [o.id for o in db.query(Owner).filter(Owner.id != "family", ~Owner.id.like(".%")).all()]
    res = scan(db, owners, today)
    held = {(h["owner"], h["isin"]) for h in res["held"]}
    changed = {"coupons": 0, "subscriptions": 0}

    def put(t: Transaction, category: str, sub: str, isin: str | None) -> bool:
        if t.category_source == "user":
            return False
        want = (category, sub, isin, "trade")
        if (t.category, t.subcategory, t.isin, t.category_source) == want:
            return False
        t.category, t.subcategory, t.isin, t.category_source, t.needs_review = category, sub, isin, "trade", False
        return True

    for s in res["streams"]:
        for c in s["coupons"]:
            t = db.get(Transaction, c["txn_id"])
            changed["coupons"] += put(t, "income", "sgb_coupon", s["isin"] if (s["owner"], s["isin"]) in held else None)
    rows = _bank_rows(db, owners)
    for (owner, isin), units in [((h["owner"], h["isin"]), D(str(h["units"]))) for h in res["held"]]:
        hit = subscription(db, owner, tranche(isin), units, rows)
        if hit:
            changed["subscriptions"] += put(hit["txn"], "investment", "sgb_gold", isin)
    db.flush()
    return changed
