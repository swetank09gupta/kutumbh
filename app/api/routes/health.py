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


@router.get("/continuity", summary="Does each bank account's printed balance chain day to day? Every break is a lost/merged/missing transaction")
def get_continuity(db: Annotated[Session, Depends(get_db)]):
    from app.txn.continuity import account_breaks
    return [{**a, "breaks": [{"date": str(b["date"]), "missing": float(b["missing"])} for b in a["breaks"]]} for a in account_breaks(db)]


@router.get("/ppf", summary="Year-by-year reconciliation of every PPF: opening + deposits + interest = closing")
def get_ppf_reconciliation(db: Annotated[Session, Depends(get_db)]):
    from app.engine.deposit_flows import ppf_measure
    from app.engine.deposits import deposit_history
    from app.engine.ppf_from_transfers import _ppf_contribution_txns_for, ppf_candidate_transfers
    from app.models.schemas import ManualAsset
    series, _ = deposit_history(db)
    out = []
    for a in db.query(ManualAsset).filter(ManualAsset.asset_type == "ppf").all():
        snaps = [(d, p) for d, p, _x in series.get((a.owner_id, a.name), [])]
        if not snaps:
            continue
        bank = [(d, amt) for d, amt, _desc, _inst in _ppf_contribution_txns_for(db, a.owner_id)]
        m = ppf_measure(snaps, bank, maybe=ppf_candidate_transfers(db, a.owner_id))
        out.append({"owner": a.owner_id, "name": a.name, "statements": len(snaps),
                    "measured": bool(m), "years": m["years"] if m else [], "notes": m["notes"] if m else
                    ["no two consecutive 31-March statements: nothing can be measured yet"],
                    "measured_to": str(m["terminal"][0]) if m else None})
    return out
