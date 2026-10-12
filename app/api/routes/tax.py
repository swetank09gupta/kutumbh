"""/api/tax — capital gains, investment income, TDS thresholds and unrealised tax per person, from proven data."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.engine import tax_view
from app.ledger import dividends

router = APIRouter(prefix="/api/tax", tags=["tax"])


@router.get("/", summary="Realised gains per FY, interest/dividend/coupon income, TDS thresholds, tax if sold today")
def get_tax(db: Annotated[Session, Depends(get_db)]):
    return tax_view.build(db)


@router.get("/dividends", summary="Dividend credits matched to a stock and ex-date by arithmetic, and what could not be matched")
def get_dividends(db: Annotated[Session, Depends(get_db)]):
    return dividends.scan(db)
