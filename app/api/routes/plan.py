"""/api/plan — goal numbers derived from real spending, real history and simulation (docs/PLANNING.md)."""
from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.planning import history, plan
from app.planning.assumptions import SCENARIOS

router = APIRouter(prefix="/api/plan", tags=["plan"])
_CACHE: dict = {}
TTL_S = 600


@router.get("/", summary="Probability, SIP-for-confidence, re-based target options and household retirement, from history + your numbers")
def get_plan(db: Annotated[Session, Depends(get_db)],
             scenario: str = Query("base", description="cautious | base | hopeful (market assumptions)"),
             stepup: float = Query(plan.DEFAULT_STEPUP, ge=0, le=0.20, description="yearly rise of savings, e.g. 0.08"),
             spend: float | None = Query(None, gt=0, description="override retirement spending, ₹ a year in today's money"),
             refresh: bool = False):
    if scenario not in SCENARIOS:
        raise HTTPException(422, f"scenario must be one of {list(SCENARIOS)}")
    key = (scenario, round(stepup, 4), spend)
    hit = _CACHE.get(key)
    if hit and not refresh and time.time() - hit[0] < TTL_S:
        return hit[1]
    try:
        out = plan.build(db, scenario, stepup, spend_today=spend, refresh_history=refresh)
    except history.PlanningDataUnavailable as exc:
        raise HTTPException(503, f"Market history is unavailable and nothing is cached yet: {exc}")
    _CACHE[key] = (time.time(), out)
    return out
