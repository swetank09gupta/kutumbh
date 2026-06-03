"""
/api/portfolio — money-weighted (XIRR) and time-weighted (CAGR) returns
across an owner's full investment portfolio.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.engine.portfolio_xirr import compute_owner_returns

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


@router.get("/returns", summary="Owner-level XIRR + CAGR with per-class breakdown")
def get_returns(
    owner_id: str,
    db: Annotated[Session, Depends(get_db)],
):
    """
    Computes XIRR (money-weighted) and CAGR (time-weighted, on totals) across
    every investing account and manual asset for the owner.

    Bank savings/current accounts and assets without dated cashflows are excluded
    and listed under `skipped`.
    """
    return compute_owner_returns(db, owner_id)
