"""
/api/transactions — read & filter transactions.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.schemas import Account, Owner, Transaction

router = APIRouter(prefix="/api/transactions", tags=["transactions"])


def _owner_ids(db: Session, owner_id: Optional[str]) -> list[str] | None:
    """Expand 'family' to all real owner IDs; return None when no filter needed."""
    if not owner_id:
        return None
    if owner_id == "family":
        return [r[0] for r in db.query(Owner.id).filter(~Owner.id.like(".%")).all()]
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
                "category_source":  t.category_source,
                "needs_review":     bool(t.needs_review),
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
    """Spending by category for a date range — the shared `expense` definition (app.semantic)."""
    ids = _owner_ids(db, owner_id)
    where, params = ["flow = 'expense'"], {}
    if ids:
        where.append("owner_id = ANY(:ids)"); params["ids"] = ids
    if from_date:
        where.append("date >= :f"); params["f"] = from_date
    if to_date:
        where.append("date <= :t"); params["t"] = to_date
    clause = " AND ".join(where)
    rows = db.execute(text(
        f"SELECT category, subcategory, COUNT(*) AS cnt, SUM(ABS(amount)) AS total "
        f"FROM v_cashflow WHERE {clause} GROUP BY category, subcategory"), params).all()

    cats: dict[str, dict] = {}
    for r in rows:
        cat = r.category or "other"
        c = cats.setdefault(cat, {"category": cat, "total": 0.0, "count": 0, "subcategories": []})
        amt, cnt = float(r.total or 0), int(r.cnt or 0)
        c["total"] += amt
        c["count"] += cnt
        if r.subcategory:
            c["subcategories"].append({"subcategory": r.subcategory, "total": round(amt, 2), "count": cnt})
    grand_total = sum(c["total"] for c in cats.values())
    result = sorted(cats.values(), key=lambda x: x["total"], reverse=True)
    for c in result:
        c["pct"] = round(c["total"] / grand_total * 100, 1) if grand_total else 0
        c["total"] = round(c["total"], 2)
        c["subcategories"].sort(key=lambda s: s["total"], reverse=True)
    d_min, d_max = db.execute(text(f"SELECT MIN(date), MAX(date) FROM v_cashflow WHERE {clause}"), params).one()
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
    """
    Monthly income vs expense by category — the shared definitions (app.semantic): income is
    salary/interest/refunds, expense excludes transfers, investments and loan prepayments.
    Rows keep the legacy shape (type credit = income, debit = expense) so existing charts work.
    """
    from datetime import timedelta
    ids = _owner_ids(db, owner_id)
    params: dict = {"cutoff": date.today() - timedelta(days=months * 31)}
    owner_clause = ""
    if ids:
        owner_clause, params["ids"] = " AND owner_id = ANY(:ids)", ids
    rows = db.execute(text(
        "SELECT to_char(month, 'YYYY-MM') AS m, category, flow, SUM(amount) AS total FROM v_cashflow "
        f"WHERE date >= :cutoff AND flow IN ('income','expense'){owner_clause} GROUP BY 1, 2, 3"), params).all()
    return [{"month": r.m, "category": r.category, "type": "credit" if r.flow == "income" else "debit",
             "total": float(r.total)} for r in rows]


@router.get("/cashflow")
def cashflow(
    db:       Annotated[Session, Depends(get_db)],
    owner_id: Optional[str] = None,
    months:   int = 12,
):
    """Month-by-month income, expense, loan prepayment, invested/redeemed and other credits."""
    from datetime import timedelta
    ids = _owner_ids(db, owner_id)
    params: dict = {"cutoff": date.today().replace(day=1) - timedelta(days=months * 31)}
    owner_clause = ""
    if ids:
        owner_clause, params["ids"] = " AND owner_id = ANY(:ids)", ids
    rows = db.execute(text(
        "SELECT to_char(month,'YYYY-MM') AS m, SUM(income) AS income, SUM(expense) AS expense, "
        "SUM(prepayment) AS prepayment, SUM(invested) AS invested, SUM(redeemed) AS redeemed, "
        f"SUM(other_credit) AS other_credit FROM v_cashflow_monthly WHERE month >= :cutoff{owner_clause} "
        "GROUP BY 1 ORDER BY 1"), params).all()
    return [{k: (float(v) if k != "m" else v) for k, v in r._mapping.items()} | {"month": r.m} for r in rows]


# ── Review: payees nobody has labelled ───────────────────────────────────────
@router.get("/review")
def review(db: Annotated[Session, Depends(get_db)], limit: int = 50):
    """Unlabelled payees, biggest first, with the local model's suggestion when it has one."""
    from app.txn.pipeline import review_list
    return review_list(db, limit=limit)


@router.post("/review/suggest")
def suggest(db: Annotated[Session, Depends(get_db)], limit: int = 24):
    """Ask the local model (~3 s per payee) to pre-fill suggestions. Nothing is applied until you confirm."""
    from app.txn.pipeline import suggest_labels
    return suggest_labels(db, limit=min(limit, 60))


class PayeeLabel(BaseModel):
    payee_key: str
    category: str
    subcategory: Optional[str] = None


@router.post("/review/label")
def label(body: PayeeLabel, db: Annotated[Session, Depends(get_db)]):
    """Label a payee once: applied to every past and future transaction with that payee."""
    from app.llm.categorise import TAXONOMY
    allowed = set(TAXONOMY) | {"income", "emi", "investment", "transfer", "family_support"}
    if body.category not in allowed:
        raise HTTPException(400, f"category must be one of {sorted(allowed)}")
    from app.txn.pipeline import label_payee
    return label_payee(db, body.payee_key, body.category, body.subcategory)


@router.get("/categories")
def categories():
    from app.llm.categorise import TAXONOMY
    out = dict(TAXONOMY)
    out.update({"income": ["salary", "interest_dividend", "refund"], "emi": ["loan_prepayment"],
                "investment": [], "transfer": ["own_account"]})
    return out
