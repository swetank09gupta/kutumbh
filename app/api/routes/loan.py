"""/api/loan — the home loan reconciled against the lender's letters, with payoff, forecast and tax use."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.engine import loan_report
from app.models.schemas import LoanAnchor

router = APIRouter(prefix="/api/loan", tags=["loan"])


@router.get("/", summary="Balance, payoff, interest saved, repo-linked rate forecast, letter reconciliation, 24(b)/80C use")
def get_loans(db: Annotated[Session, Depends(get_db)]):
    out = []
    for (owner_id,) in db.query(LoanAnchor.owner_id).distinct().all():
        rep = loan_report.report(db, owner_id)
        if rep:
            out.append({**rep, "tax": loan_report.tax(db, owner_id), "invest_comparison": _invest_comparison(db, owner_id, rep)})
    return {"loans": out}


def _invest_comparison(db, owner_id: str, rep: dict):
    """Prepay or invest, on the real history and the borrower's own tax (app/planning/prepay.py); None while planning is unavailable."""
    try:
        from app.planning import goal_plan, prepay
        ctx = goal_plan.get_context(db)
        return prepay.compare(ctx, float(rep["rate"]), owner_id) if ctx else None
    except Exception:
        return None
