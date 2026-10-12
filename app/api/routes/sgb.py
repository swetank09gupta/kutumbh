"""/api/sgb — Sovereign Gold Bonds as the bank statements prove them: coupons, cost paid, bonds no statement lists."""
from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.engine.networth import real_owners
from app.ledger import sgb_evidence
from app.ledger.sgb import tranche

router = APIRouter(prefix="/api/sgb", tags=["sgb"])


def summary(db: Session, today: date | None = None) -> dict:
    from app.engine.holdings_view import latest_holdings
    from decimal import Decimal
    owners = [o.id for o in real_owners(db)]
    res = sgb_evidence.scan(db, owners, today)
    per_gram = None                                          # today's price per gram, from any SGB holding we value
    for h in latest_holdings(db, owner_ids=owners):
        if tranche(h.isin) and float(h.units or 0) > 0:
            per_gram = float(h.current_value or 0) / float(h.units)
            break
    for owner in owners:
        paid = sgb_evidence.paid_terms(db, owner)
        for s in res["streams"]:
            if s["owner"] != owner or not s["held"]:
                continue
            hit = paid.get(s["isin"])
            s["cost"] = ({"source": "bank subscription debit", "date": hit[0].isoformat(), "per_gram": float(hit[1]),
                          "total": float(hit[1] * hit[2])} if hit else
                         {"source": "nominal issue price assumed, no subscription debit found",
                          "date": tranche(s["isin"]).issue_date.isoformat(), "per_gram": s["nominal"],
                          "total": s["nominal"] * s["units"],
                          "note": "online applicants paid ₹50 a gram less"})
    for s in res["found_not_held"]:
        s["estimated_value"] = round(per_gram * s["units"], 2) if per_gram else None
        s["note"] = ("coupons of exactly units x nominal x 1.25% arrive for a bond no statement lists; "
                     "add it in Manual Assets (asset type SGB, name containing the series) to count it")
    return res


@router.get("/", summary="SGB coupons matched to the statements, cost paid, missing coupons, bonds only the coupons reveal")
def get_sgb(db: Annotated[Session, Depends(get_db)]):
    return summary(db)
