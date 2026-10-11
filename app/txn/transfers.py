"""
Own-account transfers.

Money moved between two of the family's own accounts is neither income nor spending. The bank
only shows a debit on one side and a credit on the other, often with unrelated narrations, so the
pair is recovered by matching: equal amount, one day apart, different accounts, and each side has
exactly one counterpart (an ambiguous match is left alone rather than guessed).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Sequence

MIN_AMOUNT = 1000          # below this a coincidental equal amount is too likely
MAX_DAYS = 1
PROTECTED_CATEGORIES = ("investment", "income")      # never re-tag these as transfers
PROTECTED_SOURCES = ("user", "trade")


@dataclass(frozen=True)
class TRow:
    id: str
    account_id: str
    date: object
    amount: float
    type: str
    category: str
    source: str


def pair_transfers(rows: Sequence[TRow]) -> list[tuple[str, str]]:
    """[(debit id, credit id)] for unambiguous own-account pairs not already tagged on both sides."""
    eligible = [r for r in rows
                if r.amount >= MIN_AMOUNT and r.category not in PROTECTED_CATEGORIES
                and r.source not in PROTECTED_SOURCES]
    by_amount: dict[float, list[TRow]] = defaultdict(list)
    for r in eligible:
        by_amount[round(r.amount, 2)].append(r)

    out: list[tuple[str, str]] = []
    for group in by_amount.values():
        debits = [r for r in group if r.type == "debit"]
        credits = [r for r in group if r.type == "credit"]
        if not debits or not credits:
            continue

        def partners(r: TRow, pool: list[TRow]) -> list[TRow]:
            return [p for p in pool if p.account_id != r.account_id
                    and abs((p.date - r.date).days) <= MAX_DAYS]

        for d in debits:
            cs = partners(d, credits)
            if len(cs) != 1:
                continue
            c = cs[0]
            if len(partners(c, debits)) != 1:
                continue
            if d.category == "transfer" and c.category == "transfer":
                continue                      # already right
            out.append((d.id, c.id))
    return out


def pair_own_transfers(db, apply: bool = True) -> dict:
    import uuid
    from app.models.schemas import Account, Transaction

    q = (db.query(Transaction)
         .join(Account, Transaction.account_id == Account.id)
         .filter(Account.account_type == "bank"))
    txns = {str(t.id): t for t in q.all()}
    rows = [TRow(i, str(t.account_id), t.date, float(t.amount), t.transaction_type,
                 t.category or "", t.category_source or "") for i, t in txns.items()]
    pairs = pair_transfers(rows)
    moved_out = moved_in = 0.0
    for d_id, c_id in pairs:
        for tid in (d_id, c_id):
            t = txns[tid]
            if t.transaction_type == "debit" and t.category != "transfer":
                moved_out += float(t.amount)
            if t.transaction_type == "credit" and t.category != "transfer":
                moved_in += float(t.amount)
            if apply:
                t.category, t.subcategory = "transfer", "own_account"
                t.category_source, t.needs_review = "paired", False
    if apply and pairs:
        db.commit()
    return {"applied": apply, "pairs": len(pairs),
            "debit_amount_retagged": round(moved_out, 2), "credit_amount_retagged": round(moved_in, 2)}
