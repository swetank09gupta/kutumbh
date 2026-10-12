"""/api/deadlines — what is coming up, and the rule or evidence behind each date."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.engine import deadlines

router = APIRouter(prefix="/api/deadlines", tags=["deadlines"])


@router.get("/", summary="PPF deposit dates and headroom, deposit maturities, ELSS unlocks, SGB maturity, loan repo watch")
def get_deadlines(db: Annotated[Session, Depends(get_db)], horizon_days: int = deadlines.HORIZON_DAYS):
    return deadlines.build(db, horizon_days=max(30, min(horizon_days, 1830)))
