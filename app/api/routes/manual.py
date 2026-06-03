"""
/api/manual — CRUD for manually-entered assets (PPF, NPS, gold, FDs without email statements).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.schemas import ManualAsset, ManualAssetContribution, Owner

router = APIRouter(prefix="/api/manual", tags=["manual"])

METAL_TYPES = {"gold", "silver", "sgb"}


class AssetCreate(BaseModel):
    owner_id:        str
    asset_type:      str
    institution:     str
    name:            str
    current_value:   float
    invested_amount: Optional[float] = None
    interest_rate:   Optional[float] = None
    quantity:        Optional[float] = None   # grams or SGB units
    metal_purity:    Optional[str]   = None   # '24K' | '22K' | '18K'
    start_date:      Optional[date]  = None
    maturity_date:   Optional[date]  = None
    is_liquid:       bool = False
    notes:           Optional[str]   = None


class AssetUpdate(BaseModel):
    current_value:   Optional[float] = None
    invested_amount: Optional[float] = None
    interest_rate:   Optional[float] = None
    quantity:        Optional[float] = None
    metal_purity:    Optional[str]   = None
    maturity_date:   Optional[date]  = None
    is_liquid:       Optional[bool]  = None
    notes:           Optional[str]   = None


@router.get("/assets")
def list_assets(
    db:           Annotated[Session, Depends(get_db)],
    owner_id:     Optional[str] = None,
    include_matured: bool = False,
):
    from datetime import date
    from sqlalchemy import or_
    q = db.query(ManualAsset)
    if owner_id:
        q = q.filter_by(owner_id=owner_id)
    if not include_matured:
        q = q.filter(
            or_(
                ManualAsset.asset_type.notin_(["fd", "rd"]),
                ManualAsset.maturity_date.is_(None),
                ManualAsset.maturity_date >= date.today(),
            )
        )
    return [_asset_dict(a) for a in q.all()]


@router.post("/assets")
def create_asset(
    body: AssetCreate,
    db:   Annotated[Session, Depends(get_db)],
):
    if not db.get(Owner, body.owner_id):
        db.add(Owner(id=body.owner_id, name=body.owner_id.capitalize()))
        db.flush()

    asset = ManualAsset(
        owner_id=body.owner_id,
        asset_type=body.asset_type,
        institution=body.institution,
        name=body.name,
        current_value=Decimal(str(body.current_value)),
        invested_amount=Decimal(str(body.invested_amount)) if body.invested_amount else None,
        interest_rate=Decimal(str(body.interest_rate)) if body.interest_rate else None,
        quantity=Decimal(str(body.quantity)) if body.quantity else None,
        metal_purity=body.metal_purity or None,
        start_date=body.start_date,
        maturity_date=body.maturity_date,
        is_liquid=body.is_liquid,
        notes=body.notes,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return _asset_dict(asset)


@router.put("/assets/{asset_id}")
def update_asset(
    asset_id: str,
    body:     AssetUpdate,
    db:       Annotated[Session, Depends(get_db)],
):
    asset = db.query(ManualAsset).filter_by(id=asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    for field, val in body.model_dump(exclude_none=True).items():
        if isinstance(val, float):
            val = Decimal(str(val))
        setattr(asset, field, val)

    db.commit()
    db.refresh(asset)
    return _asset_dict(asset)


@router.delete("/assets/{asset_id}")
def delete_asset(
    asset_id: str,
    db:       Annotated[Session, Depends(get_db)],
):
    asset = db.query(ManualAsset).filter_by(id=asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    db.delete(asset)
    db.commit()
    return {"deleted": asset_id}


@router.get("/metal-prices")
def metal_prices():
    """
    Current gold and silver spot prices in INR per gram.
    Sourced from Yahoo Finance (COMEX futures + USDINR), cached 6 hours.
    """
    from app.engine.metal_prices import get_metal_prices
    return get_metal_prices()


@router.get("/metal-history")
def metal_price_history():
    """
    7-year monthly gold/silver price history in INR/gram, plus 1Y/3Y/5Y/7Y CAGR.
    Cached 24 hours.
    """
    from app.engine.metal_prices import get_metal_price_history
    return get_metal_price_history()


@router.post("/refresh-metals")
def refresh_metal_prices(db: Annotated[Session, Depends(get_db)]):
    """
    Recompute current_value for all gold/silver/SGB assets that have a quantity set,
    using today's spot prices.
    """
    from app.engine.metal_prices import compute_asset_cagr, get_metal_prices, price_for_asset

    prices = get_metal_prices()
    if "error" in prices:
        raise HTTPException(status_code=503, detail=prices["error"])

    metal_assets = db.query(ManualAsset).filter(
        ManualAsset.asset_type.in_(list(METAL_TYPES))
    ).all()

    updated = 0
    details = []
    for asset in metal_assets:
        qty = float(asset.quantity or 0)
        if qty <= 0:
            continue
        purity = asset.metal_purity or ("22K" if asset.asset_type == "gold" else "24K")
        new_val = price_for_asset(asset.asset_type, qty, purity)
        if new_val > 0:
            asset.current_value = Decimal(str(new_val))
            updated += 1
            invested = float(asset.invested_amount or 0)
            total_return = compute_asset_cagr(asset.asset_type, qty, invested, purity) if invested > 0 else None
            details.append({
                "id":           str(asset.id),
                "name":         asset.name,
                "new_value":    new_val,
                "total_return_pct": total_return,
            })

    if updated:
        db.commit()

    return {
        "updated": updated,
        "details": details,
        "prices": {
            "gold_24k": prices.get("gold_24k_inr_per_gram"),
            "gold_22k": prices.get("gold_22k_inr_per_gram"),
            "silver":   prices.get("silver_inr_per_gram"),
            "sgb":      prices.get("sgb_inr_per_unit"),
            "fetched_at": prices.get("fetched_at"),
        },
    }


def _compute_returns(a: ManualAsset) -> dict:
    """
    Compute CAGR / XIRR for a manual asset depending on type.
    Returns a dict with keys: cagr_pct, xirr_pct, total_return_pct.
    """
    from app.engine.xirr_engine import (
        build_investment_cashflows, cagr, total_return_pct, xirr,
    )
    today = date.today()
    current  = float(a.current_value or 0)
    invested = float(a.invested_amount or 0)
    rate     = float(a.interest_rate or 0)

    result: dict = {"cagr_pct": None, "xirr_pct": None, "total_return_pct": None}

    if current <= 0 or invested <= 0:
        return result

    result["total_return_pct"] = round((current / invested - 1) * 100, 2)

    # ── Interest-bearing (FD / RD / PPF / NPS) ───────────────────────────────
    if a.asset_type in ("fd", "rd", "ppf", "nps", "insurance") and a.start_date:
        years = (today - a.start_date).days / 365.25
        result["cagr_pct"] = cagr(invested, current, years)

        # If there are recorded contributions, compute proper XIRR
        contribs = sorted(a.contributions, key=lambda c: c.date)
        if contribs:
            cfs = build_investment_cashflows(
                [{"date": c.date, "amount": float(c.amount), "transaction_type": "debit"}
                 for c in contribs],
                current,
                today,
            )
            result["xirr_pct"] = xirr(cfs)
        elif a.start_date and invested > 0:
            # Single-lump approximation
            cfs = [
                (a.start_date, -invested),
                (today,         current),
            ]
            result["xirr_pct"] = xirr(cfs)

    # ── Metals (gold / silver / sgb) ─────────────────────────────────────────
    elif a.asset_type in ("gold", "silver", "sgb") and a.start_date:
        years = (today - a.start_date).days / 365.25
        result["cagr_pct"] = cagr(invested, current, years)
        # XIRR = same as lump-sum CAGR for metals (single buy)
        cfs = [(a.start_date, -invested), (today, current)]
        result["xirr_pct"] = xirr(cfs)

    # ── Other (real_estate, other) ────────────────────────────────────────────
    elif a.start_date:
        years = (today - a.start_date).days / 365.25
        result["cagr_pct"] = cagr(invested, current, years)

    return result


def _asset_dict(a: ManualAsset) -> dict:
    returns = _compute_returns(a)
    return {
        "id":               str(a.id),
        "owner_id":         a.owner_id,
        "asset_type":       a.asset_type,
        "institution":      a.institution,
        "name":             a.name,
        "current_value":    float(a.current_value or 0),
        "invested_amount":  float(a.invested_amount or 0),
        "interest_rate":    float(a.interest_rate or 0),
        "quantity":         float(a.quantity) if a.quantity else None,
        "metal_purity":     a.metal_purity,
        "start_date":       str(a.start_date) if a.start_date else None,
        "maturity_date":    str(a.maturity_date) if a.maturity_date else None,
        "is_liquid":        bool(a.is_liquid),
        "notes":            a.notes,
        "last_updated":     str(a.last_updated) if a.last_updated else None,
        "cagr_pct":         returns["cagr_pct"],
        "xirr_pct":         returns["xirr_pct"],
        "total_return_pct": returns["total_return_pct"],
    }


# ── Contributions (PPF / NPS multi-deposit XIRR) ──────────────────────────

class ContributionCreate(BaseModel):
    date:   date
    amount: float
    notes:  Optional[str] = None


@router.get("/assets/{asset_id}/contributions")
def list_contributions(asset_id: str, db: Annotated[Session, Depends(get_db)]):
    asset = db.query(ManualAsset).filter_by(id=asset_id).first()
    if not asset:
        raise HTTPException(404, "Asset not found")
    return [
        {"id": str(c.id), "date": str(c.date), "amount": float(c.amount), "notes": c.notes}
        for c in asset.contributions
    ]


@router.post("/assets/{asset_id}/contributions")
def add_contribution(
    asset_id: str,
    body:     ContributionCreate,
    db:       Annotated[Session, Depends(get_db)],
):
    asset = db.query(ManualAsset).filter_by(id=asset_id).first()
    if not asset:
        raise HTTPException(404, "Asset not found")
    c = ManualAssetContribution(
        asset_id=asset.id,
        date=body.date,
        amount=Decimal(str(body.amount)),
        notes=body.notes,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return {"id": str(c.id), "date": str(c.date), "amount": float(c.amount), "notes": c.notes}


@router.delete("/assets/{asset_id}/contributions/{contrib_id}")
def delete_contribution(
    asset_id:   str,
    contrib_id: str,
    db:         Annotated[Session, Depends(get_db)],
):
    c = db.query(ManualAssetContribution).filter_by(id=contrib_id, asset_id=asset_id).first()
    if not c:
        raise HTTPException(404, "Contribution not found")
    db.delete(c)
    db.commit()
    return {"deleted": contrib_id}
