"""
Reconcile bank alert e-mails against statement rows.

Alerts are evidence, never cashflow. For every alert we answer one question: is there a
statement row for it?

    matched   — a statement row confirms it (by UTR/UPI reference, else same day ±2, amount, direction)
    outside   — older than the first statement we hold: proves nothing
    untracked — an account that appears in alerts but whose statements we never receive
    pending   — newer than the last statement we hold for that bank: nothing to compare with yet
    unmatched — the statement covers that date but has no such row: a parser gap or a missing page
    dup       — the same event announced twice (e.g. SBI sends a CBSSBI alert and an NEFT/SI mail)

Matching is greedy and one-to-one, so two identical payments need two statement rows.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.schemas import Account, BankAlertRow, Transaction


def _tokens(ref: str | None) -> list[str]:
    return [t for t in re.split(r"[^A-Za-z0-9]+", ref or "") if len(t) >= 10 and re.search(r"\d{6}", t)]


def reconcile_alerts(db: Session, apply: bool = True) -> dict:
    alerts = db.query(BankAlertRow).order_by(BankAlertRow.txn_date, BankAlertRow.sent_at).all()
    if not alerts:
        return {"alerts": 0, "matched": 0, "pending": 0, "unmatched": 0, "dup": 0, "outside": 0, "untracked": 0}

    # statement coverage per (owner, bank)
    cover: dict[tuple[str, str], tuple] = {}
    for owner, bank, lo, hi in (db.query(Account.owner_id, Account.institution, func.min(Transaction.date),
                                         func.max(Transaction.date))
                                .join(Transaction, Transaction.account_id == Account.id)
                                .filter(Account.account_type == "bank")
                                .group_by(Account.owner_id, Account.institution).all()):
        cover[(owner, (bank or "").upper())] = (lo, hi)

    txns: dict[tuple[str, str], list[Transaction]] = defaultdict(list)
    for t, owner, bank in (db.query(Transaction, Account.owner_id, Account.institution)
                           .join(Account, Account.id == Transaction.account_id)
                           .filter(Account.account_type == "bank").all()):
        txns[(owner, (bank or "").upper())].append(t)

    # 1. the same event announced twice: a ref-less alert is a copy when a ref alert already tells it
    groups: dict[tuple, list[BankAlertRow]] = defaultdict(list)
    for a in alerts:
        groups[(a.owner_id, a.bank, a.account_last4, a.txn_date, a.direction, a.amount)].append(a)
    dups: set = set()
    for g in groups.values():
        without = [a for a in g if not a.ref]
        for a in without[:min(len(without), sum(1 for a in g if a.ref))]:
            dups.add(a.id)

    taken: set = set()
    hit: dict = {}                                    # alert id -> matched Transaction

    def find(a: BankAlertRow, by_ref: bool):
        toks = _tokens(a.ref) if by_ref else []
        for t in txns.get((a.owner_id, a.bank), []):
            if t.id in taken or t.transaction_type != a.direction \
                    or abs(Decimal(t.amount) - Decimal(a.amount)) > Decimal("0.01"):
                continue
            days = abs((t.date - a.txn_date).days)
            if by_ref:
                hay = f"{t.raw_description or ''} {t.reference_number or ''}".upper()
                if toks and days <= 4 and any(tok.upper() in hay for tok in toks):
                    return t
            elif days <= 2:
                return t
        return None

    live = [a for a in alerts if a.id not in dups]
    for by_ref in (True, False):                      # strongest evidence first, across all alerts
        for a in live:
            if a.id not in hit and (t := find(a, by_ref)) is not None:
                taken.add(t.id)
                hit[a.id] = t

    stats = {"alerts": len(alerts), "matched": 0, "pending": 0, "unmatched": 0, "dup": 0, "outside": 0, "untracked": 0}
    # an account is tracked once any of its alerts matched a statement row (SBI alerts show the
    # real account number, statements only a masked reference), or its last 4 equal a stored ref
    tracked = {(a.owner_id, a.bank, a.account_last4) for a in alerts if a.id in hit}
    tracked |= {(o, (b or "").upper(), (r or "")[-4:]) for o, b, r in
                db.query(Account.owner_id, Account.institution, Account.account_ref)
                .filter(Account.account_type == "bank").all()}
    for a in alerts:
        txn = hit.get(a.id)
        if a.id in dups:
            status = "dup"
        elif txn is not None:
            status = "matched"
        elif (a.owner_id, a.bank, a.account_last4) not in tracked:
            status = "untracked"
        else:
            lo, hi = cover.get((a.owner_id, a.bank), (None, None))
            if hi is None or a.txn_date > hi:
                status = "pending"                    # no statement reaches this date yet
            elif a.txn_date < lo:
                status = "outside"                    # older than the first statement we hold
            else:
                status = "unmatched"                  # the statement covers it but has no such row
        stats[status] += 1
        if apply:
            a.status, a.matched_txn_id = status, (txn.id if txn is not None else None)
    if apply:
        db.commit()
    return stats


def alert_summary(db: Session, recent: int = 40) -> dict:
    """What the alerts say that no statement does yet, for /api/health/alerts and the UI."""
    def rows(status, limit):
        q = db.query(BankAlertRow).filter(BankAlertRow.status == status)
        return [{"date": a.txn_date.isoformat(), "owner": a.owner_id, "bank": a.bank, "account": a.account_last4,
                 "direction": a.direction, "amount": float(a.amount), "payee": a.payee, "ref": a.ref, "kind": a.kind}
                for a in q.order_by(BankAlertRow.txn_date.desc()).limit(limit).all()]

    counts = dict(db.query(BankAlertRow.status, func.count(BankAlertRow.id)).group_by(BankAlertRow.status).all())
    pend = (db.query(BankAlertRow.bank, BankAlertRow.owner_id, BankAlertRow.direction,
                     func.count(BankAlertRow.id), func.sum(BankAlertRow.amount), func.min(BankAlertRow.txn_date))
            .filter(BankAlertRow.status == "pending").group_by(BankAlertRow.bank, BankAlertRow.owner_id,
                                                              BankAlertRow.direction).all())
    latest: dict[tuple, BankAlertRow] = {}
    for a in (db.query(BankAlertRow).filter(BankAlertRow.balance_after.isnot(None))
              .order_by(BankAlertRow.txn_date, BankAlertRow.sent_at).all()):
        latest[(a.owner_id, a.bank, a.account_last4)] = a
    return {
        "counts": counts,
        "pending_by_bank": [{"bank": b, "owner": o, "direction": d, "count": n, "amount": float(v or 0),
                             "since": str(since)} for b, o, d, n, v, since in pend],
        "pending": rows("pending", recent),
        "unmatched": rows("unmatched", recent),
        "accounts_without_statements": [
            {"owner": o, "bank": b, "account": last4, "last_balance": float(a.balance_after),
             "as_of": a.txn_date.isoformat(), "alerts": db.query(func.count(BankAlertRow.id)).filter(
                 BankAlertRow.owner_id == o, BankAlertRow.bank == b, BankAlertRow.account_last4 == last4).scalar()}
            for (o, b, last4), a in sorted(latest.items(), key=lambda kv: -float(kv[1].balance_after))
            if a.status == "untracked"],
    }
