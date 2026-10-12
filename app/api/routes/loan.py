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
            out.append({**rep, "tax": loan_report.tax(db, owner_id)})
    return {"loans": out}
