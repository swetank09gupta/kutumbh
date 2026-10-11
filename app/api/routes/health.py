"""/api/health — cross-page consistency."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import consistency
from app.api.deps import get_db

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("/consistency", summary="Do every page, card and tile agree? (fail = bug, warn = data gap)")
def get_consistency(db: Annotated[Session, Depends(get_db)]):
    return consistency.run(db)


@router.get("/alerts", summary="Bank alert e-mails vs statements: pending, unmatched, accounts without statements")
def get_alerts(db: Annotated[Session, Depends(get_db)]):
    from app.txn.alert_recon import alert_summary, reconcile_alerts
    reconcile_alerts(db)
    return alert_summary(db)
