"""
/api/ledger — instrument ledger, reconciliation and roll-forward.
See docs/adr/0023-instrument-ledger.md.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Annotated, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.ledger import engine
from app.ledger.rollforward import rollforward
from app.models.schemas import Owner, ReconIssue

router = APIRouter(prefix="/api/ledger", tags=["ledger"])


@router.post("/reconcile", summary="Run every reconciliation check and refresh recon_issues")
def run_reconcile(db: Annotated[Session, Depends(get_db)]):
    return engine.reconcile(db)


@router.post("/rollforward", summary="Rebuild today's holdings: last statement + trades since")
def run_rollforward(db: Annotated[Session, Depends(get_db)]):
    return rollforward(db)


@router.get("/summary", summary="Open reconciliation issues by kind and severity")
def summary(db: Annotated[Session, Depends(get_db)]):
    rows = (db.query(ReconIssue.kind, ReconIssue.severity, func.count())
            .filter(ReconIssue.status == "open").group_by(ReconIssue.kind, ReconIssue.severity).all())
    by_kind: dict[str, dict[str, int]] = defaultdict(dict)
    for kind, sev, n in rows:
        by_kind[kind][sev] = n
    return {
        "open": sum(n for _, _, n in rows),
        "errors": sum(n for _, s, n in rows if s == "error"),
        "by_kind": dict(sorted(by_kind.items())),
    }


@router.get("/issues", summary="List reconciliation issues")
def issues(
    db: Annotated[Session, Depends(get_db)],
    owner_id: Optional[str] = None,
    kind: Optional[str] = None,
    severity: Optional[str] = None,
    status: str = "open",
    since: Optional[str] = None,
    limit: int = 200,
):
    q = db.query(ReconIssue).filter(ReconIssue.status == status)
    if owner_id:
        q = q.filter(ReconIssue.owner_id == owner_id)
    if kind:
        q = q.filter(ReconIssue.kind == kind)
    if severity:
        q = q.filter(ReconIssue.severity == severity)
    if since:
        q = q.filter(ReconIssue.as_of_date >= since)
    rows = q.order_by(ReconIssue.severity, ReconIssue.as_of_date.desc()).limit(limit).all()
    return [{
        "owner_id": r.owner_id, "isin": r.isin, "instrument": r.instrument_name, "kind": r.kind,
        "severity": r.severity, "as_of": str(r.as_of_date) if r.as_of_date else None,
        "ledger_qty": float(r.ledger_qty) if r.ledger_qty is not None else None,
        "statement_qty": float(r.statement_qty) if r.statement_qty is not None else None,
        "detail": r.detail,
    } for r in rows]


@router.get("/positions", summary="Positions as the ledger sees them (read-only)")
def positions(db: Annotated[Session, Depends(get_db)], owner_id: Optional[str] = None):
    owners = [owner_id] if owner_id else [o for (o,) in db.query(Owner.id).filter(Owner.id != "family").all()]
    out = []
    for oid in owners:
        led = engine.compute_owner(db, oid)
        for isin, st in sorted(led.states.items()):
            p = st.position
            if p.qty <= 0 and not st.pending:
                continue
            out.append({
                "owner_id": oid, "isin": isin, "name": (led.meta.get(isin) or {}).get("name"),
                "qty": float(p.qty), "statement_qty": float(st.anchor_qty),
                "pending_qty": float(sum((t.signed for t in st.pending), Decimal(0))),
                "avg_cost": float(p.avg_cost) if p.avg_cost is not None else None,
                "traced_qty": float(p.known_qty), "untraced_qty": float(p.unknown_qty),
                "cost_quality": p.quality, "flags": p.flags,
                "first_buy_date": str(p.first_buy_date) if p.first_buy_date else None,
            })
    return out


class BrokerRow(BaseModel):
    owner_id: str
    isin: Optional[str] = None
    symbol: Optional[str] = None
    qty: float


@router.post("/verify", summary="Compare ledger positions against a list from the broker")
def verify(rows: list[BrokerRow], db: Annotated[Session, Depends(get_db)]):
    """
    POST the quantities you see in Zerodha Console / Coin (by ISIN or by symbol
    text) to get a line-by-line match / mismatch report against the ledger.
    """
    cache: dict[str, engine.OwnerLedger] = {}
    result = []
    for r in rows:
        led = cache.setdefault(r.owner_id, engine.compute_owner(db, r.owner_id))
        isin = r.isin
        if not isin and r.symbol:
            needle = r.symbol.lower()
            for i, m in led.meta.items():
                hay = " ".join(str(x or "") for x in (m.get("name"), m.get("symbol"))).lower()
                if needle in hay:
                    isin = i
                    break
        st = led.states.get(isin) if isin else None
        have = float(st.position.qty) if st else 0.0
        result.append({"owner_id": r.owner_id, "isin": isin, "symbol": r.symbol, "broker_qty": r.qty,
                       "ledger_qty": have, "match": abs(have - r.qty) < 0.001,
                       "diff": round(have - r.qty, 4)})
    return {"checked": len(result), "matched": sum(1 for x in result if x["match"]), "rows": result}


@router.post("/dedupe", summary="Remove cross-source duplicate trades and their cashflows (dry run by default)")
def dedupe(db: Annotated[Session, Depends(get_db)], apply: bool = False):
    return engine.dedupe_cashflows(db, apply=apply)
