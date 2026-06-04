"""
/api/transactions — read & filter transactions.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.schemas import Account, Owner, Transaction

router = APIRouter(prefix="/api/transactions", tags=["transactions"])


def _owner_ids(db: Session, owner_id: Optional[str]) -> list[str] | None:
    """Expand 'family' to all real owner IDs; return None when no filter needed."""
    if not owner_id:
        return None
    if owner_id == "family":
        return [r[0] for r in db.query(Owner.id).all()]
    return [owner_id]


@router.get("/")
def list_transactions(
    db:       Annotated[Session, Depends(get_db)],
    owner_id: Optional[str]  = None,
    category: Optional[str]  = None,
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
    limit:     int = 200,
    offset:    int = 0,
):
    q = db.query(Transaction).join(Account, Transaction.account_id == Account.id)

    ids = _owner_ids(db, owner_id)
    if ids:
        q = q.filter(Account.owner_id.in_(ids))
    if category:
        q = q.filter(Transaction.category == category)
    if from_date:
        q = q.filter(Transaction.date >= from_date)
    if to_date:
        q = q.filter(Transaction.date <= to_date)

    total = q.count()
    rows  = q.order_by(Transaction.date.desc()).offset(offset).limit(limit).all()

    return {
        "total": total,
        "items": [
            {
                "id":               str(t.id),
                "account_id":       str(t.account_id),
                "date":             t.date,
                "amount":           float(t.amount),
                "type":             t.transaction_type,
                "description":      t.description,
                "category":         t.category,
                "subcategory":      t.subcategory,
                "balance_after":    float(t.balance_after) if t.balance_after else None,
            }
            for t in rows
        ],
    }


@router.get("/expense-breakdown")
def expense_breakdown(
    db:        Annotated[Session, Depends(get_db)],
    owner_id:  Optional[str]  = None,
    from_date: Optional[date] = Query(None),
    to_date:   Optional[date] = Query(None),
):
    """Spending by category + subcategory for a date range (excludes transfers)."""
    from sqlalchemy import func

    q = (
        db.query(
            Transaction.category,
            Transaction.subcategory,
            func.count(Transaction.id).label("cnt"),
            func.sum(func.abs(Transaction.amount)).label("total"),
        )
        .join(Account, Transaction.account_id == Account.id)
        .filter(
            Transaction.transaction_type == "debit",
            Transaction.category.notin_(["transfer"]),
        )
    )
    ids = _owner_ids(db, owner_id)
    if ids:
        q = q.filter(Account.owner_id.in_(ids))
    if from_date:
        q = q.filter(Transaction.date >= from_date)
    if to_date:
        q = q.filter(Transaction.date <= to_date)

    rows = q.group_by(Transaction.category, Transaction.subcategory).all()

    cats: dict[str, dict] = {}
    for r in rows:
        cat = r.category or "other"
        if cat not in cats:
            cats[cat] = {"category": cat, "total": 0.0, "count": 0, "subcategories": []}
        amt = float(r.total or 0)
        cnt = int(r.cnt or 0)
        cats[cat]["total"] += amt
        cats[cat]["count"] += cnt
        if r.subcategory:
            cats[cat]["subcategories"].append(
                {"subcategory": r.subcategory, "total": round(amt, 2), "count": cnt}
            )

    grand_total = sum(c["total"] for c in cats.values())
    result = sorted(cats.values(), key=lambda x: x["total"], reverse=True)
    for c in result:
        c["pct"]   = round(c["total"] / grand_total * 100, 1) if grand_total else 0
        c["total"] = round(c["total"], 2)
        c["subcategories"].sort(key=lambda s: s["total"], reverse=True)

    # date range of actual data in this query
    date_q = (
        db.query(func.min(Transaction.date), func.max(Transaction.date))
        .join(Account, Transaction.account_id == Account.id)
        .filter(Transaction.transaction_type == "debit",
                Transaction.category.notin_(["transfer"]))
    )
    if ids:
        date_q = date_q.filter(Account.owner_id.in_(ids))
    if from_date:
        date_q = date_q.filter(Transaction.date >= from_date)
    if to_date:
        date_q = date_q.filter(Transaction.date <= to_date)
    d_min, d_max = date_q.one()

    return {
        "grand_total": round(grand_total, 2),
        "date_min": str(d_min) if d_min else None,
        "date_max": str(d_max) if d_max else None,
        "categories": result,
    }


@router.get("/summary")
def monthly_summary(
    db:       Annotated[Session, Depends(get_db)],
    owner_id: Optional[str] = None,
    months:   int = 12,
):
    """Monthly income vs expense summary."""
    from sqlalchemy import func, text
    from datetime import date, timedelta

    cutoff = date.today() - timedelta(days=months * 31)
    q = (
        db.query(
            func.date_trunc("month", Transaction.date).label("month"),
            Transaction.category,
            Transaction.transaction_type,
            func.sum(Transaction.amount).label("total"),
        )
        .join(Account, Transaction.account_id == Account.id)
        .filter(Transaction.date >= cutoff)
        # Exclude both transfers (inter-account moves are not income/expense)
        # AND investments (wealth-building outflows are savings, not consumption).
        # EMI is kept as expense — it includes interest cost which IS spend.
        .filter(Transaction.category.notin_(["transfer", "investment"]))
    )
    ids = _owner_ids(db, owner_id)
    if ids:
        q = q.filter(Account.owner_id.in_(ids))

    rows = q.group_by("month", Transaction.category, Transaction.transaction_type).all()

    return [
        {"month": str(r.month)[:7], "category": r.category,
         "type": r.transaction_type, "total": float(r.total)}
        for r in rows
    ]
