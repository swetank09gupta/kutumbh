"""
Cross-file duplicate bank rows.

The same bank transaction appears in overlapping statements (monthly vs quarterly PDFs, CSV vs
PDF) with different narration formats, so the narration-hash fingerprint cannot tell they are one
payment. The reliable identity is: same account, date, amount and direction **and** a shared
UTR/reference. Two genuinely separate payments the same day have different UTRs and are never merged.
"""
from __future__ import annotations

import itertools
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Sequence

from app.txn.normalize import share_utr

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Row:
    id: str
    account_id: str
    date: object
    amount: object
    type: str
    ref: str
    raw: str
    category: str
    balance: object = None                 # balance printed after the row, when the layout has one


def _score(r: Row) -> tuple:
    """Higher = the row we keep: a real category beats a generic one, then the richer narration."""
    informative = 0 if r.category in ("other", "transfer", "", None) else 1
    return (informative, len(r.raw or ""), -len(r.ref or ""))


def duplicate_ids(rows: Sequence[Row]) -> dict[str, str]:
    """{duplicate row id: keeper row id}; keepers are never themselves listed as duplicates."""
    groups: dict[tuple, list[Row]] = defaultdict(list)
    for r in rows:
        groups[(r.account_id, r.date, r.amount, r.type)].append(r)
    dup: dict[str, str] = {}
    for g in groups.values():
        if len(g) < 2:
            continue
        # union rows that share a UTR into clusters
        parent = {r.id: r.id for r in g}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a, b in itertools.combinations(g, 2):
            if a.balance is not None and b.balance is not None and a.balance != b.balance:
                continue                   # separate payments: the running balance moved differently
            if share_utr((a.ref, a.raw), (b.ref, b.raw)):
                parent[find(a.id)] = find(b.id)
        clusters: dict[str, list[Row]] = defaultdict(list)
        for r in g:
            clusters[find(r.id)].append(r)
        for members in clusters.values():
            if len(members) < 2:
                continue
            keeper = max(members, key=_score)
            for m in members:
                if m.id != keeper.id:
                    dup[m.id] = keeper.id
    return dup


def dedupe_bank_transactions(db, apply: bool = True) -> dict:
    """Remove cross-file duplicate bank rows (derived data: a reprocess recreates and re-removes them)."""
    from app.models.schemas import Account, Transaction

    rows = (db.query(Transaction, Account.id)
            .join(Account, Transaction.account_id == Account.id)
            .filter(Account.account_type == "bank").all())
    plain = [Row(str(t.id), str(aid), t.date, t.amount, t.transaction_type,
                 t.reference_number or "", t.raw_description or t.description or "", t.category or "", t.balance_after)
             for t, aid in rows]
    dups = duplicate_ids(plain)
    amount = sum(float(r.amount) for r in plain if r.id in dups)
    if apply and dups:
        import uuid
        db.query(Transaction).filter(Transaction.id.in_([uuid.UUID(i) for i in dups])) \
            .delete(synchronize_session=False)
        db.commit()
    return {"applied": apply, "duplicates": len(dups), "amount": round(amount, 2)}
