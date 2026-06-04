"""
/api/profile — Owner profile CRUD (DOB, risk appetite, retirement age).
Used by the AI advisor and recommendation engine.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.schemas import Owner

router = APIRouter(prefix="/api/profile", tags=["profile"])


class ProfileUpdate(BaseModel):
    name:           Optional[str]   = None
    birth_date:     Optional[date]  = None
    risk_appetite:  Optional[str]   = None   # conservative | moderate | aggressive
    retirement_age: Optional[int]   = None
    tax_regime:     Optional[str]   = None   # "old" | "new"
    epf_monthly:    Optional[float] = None   # ₹ / month
    home_loan_share_pct: Optional[float] = None   # % co-borrower share


@router.get("/")
def list_profiles(db: Annotated[Session, Depends(get_db)]):
    owners = db.query(Owner).filter(Owner.id != "family").order_by(Owner.id).all()
    return [_dict(o) for o in owners]


@router.get("/{owner_id}")
def get_profile(owner_id: str, db: Annotated[Session, Depends(get_db)]):
    o = db.get(Owner, owner_id)
    if not o:
        raise HTTPException(404, "Owner not found")
    return _dict(o)


@router.put("/{owner_id}")
def update_profile(
    owner_id: str,
    body: ProfileUpdate,
    db:   Annotated[Session, Depends(get_db)],
):
    o = db.get(Owner, owner_id)
    if not o:
        raise HTTPException(404, "Owner not found")
    for field, val in body.model_dump(exclude_none=True).items():
        setattr(o, field, val)
    db.commit()
    db.refresh(o)
    return _dict(o)


def _dict(o: Owner) -> dict:
    bd      = getattr(o, "birth_date", None)
    risk    = getattr(o, "risk_appetite", None)
    ret_age = getattr(o, "retirement_age", None)
    today   = date.today()
    age     = (today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
               if bd else None)
    return {
        "id":             o.id,
        "name":           o.name,
        "birth_date":     str(bd) if bd else None,
        "age":            age,
        "risk_appetite":  risk or "moderate",
        "retirement_age": ret_age or 60,
        "tax_regime":     getattr(o, "tax_regime", None) or "old",
        "epf_monthly":    float(getattr(o, "epf_monthly", 0) or 0),
        "home_loan_share_pct": float(getattr(o, "home_loan_share_pct", 0) or 0),
    }
