"""
/api/goals — CRUD for financial goals + projections + recommendations.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.engine.goals import project_all_goals, project_goal, update_goal_sip, refresh_corpus_from_networth
from app.engine.recommendations import generate as gen_recs
from app.models.schemas import Goal

router = APIRouter(prefix="/api/goals", tags=["goals"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────
class GoalCreate(BaseModel):
    owner_id:             str
    name:                 str
    goal_type:            str
    target_amount:        float
    target_date:          date
    current_corpus:       float = 0.0
    monthly_sip_needed:   Optional[float] = None
    expected_return_rate: float = 12.0
    inflation_rate:       float = 6.0
    notes:                Optional[str] = None


class GoalUpdate(BaseModel):
    name:                 Optional[str]   = None
    target_amount:        Optional[float] = None
    target_date:          Optional[date]  = None
    current_corpus:       Optional[float] = None
    expected_return_rate: Optional[float] = None
    inflation_rate:       Optional[float] = None
    notes:                Optional[str]   = None


# ── Routes ────────────────────────────────────────────────────────────────────
@router.get("/")
def list_goals(
    db:       Annotated[Session, Depends(get_db)],
    owner_id: Optional[str] = None,
):
    q = db.query(Goal)
    if owner_id:
        q = q.filter(Goal.owner_id.in_([owner_id, "family"]))
    goals = q.order_by(Goal.target_date).all()
    return [_goal_dict(g) for g in goals]


@router.post("/")
def create_goal(
    body: GoalCreate,
    db:   Annotated[Session, Depends(get_db)],
):
    goal = Goal(
        owner_id=body.owner_id,
        name=body.name,
        goal_type=body.goal_type,
        target_amount=Decimal(str(body.target_amount)),
        target_date=body.target_date,
        current_corpus=Decimal(str(body.current_corpus)),
        monthly_sip_needed=Decimal(str(body.monthly_sip_needed)) if body.monthly_sip_needed else None,
        expected_return_rate=Decimal(str(body.expected_return_rate)),
        inflation_rate=Decimal(str(body.inflation_rate)),
        notes=body.notes,
    )
    db.add(goal)
    db.commit()
    db.refresh(goal)
    update_goal_sip(db, str(goal.id))
    return _goal_dict(goal)


@router.put("/{goal_id}")
def update_goal(
    goal_id: str,
    body:    GoalUpdate,
    db:      Annotated[Session, Depends(get_db)],
):
    goal = db.query(Goal).filter_by(id=goal_id).first()
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")

    for field, val in body.model_dump(exclude_none=True).items():
        if isinstance(val, float):
            val = Decimal(str(val))
        setattr(goal, field, val)

    db.commit()
    db.refresh(goal)
    update_goal_sip(db, goal_id)
    return _goal_dict(goal)


@router.delete("/{goal_id}")
def delete_goal(
    goal_id: str,
    db:      Annotated[Session, Depends(get_db)],
):
    goal = db.query(Goal).filter_by(id=goal_id).first()
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    db.delete(goal)
    db.commit()
    return {"deleted": goal_id}


@router.get("/projections")
def goal_projections(
    db:       Annotated[Session, Depends(get_db)],
    owner_id: Optional[str] = None,
):
    projections = project_all_goals(db, owner_id)
    return [
        {
            "goal_id":              p.goal_id,
            "name":                 p.name,
            "goal_type":            p.goal_type,
            "target_date":          str(p.target_date),
            "inflation_adj_target": p.inflation_adj_target,
            "current_corpus":       p.current_corpus,
            "monthly_sip_needed":   p.monthly_sip_needed,
            "projected_corpus":     p.projected_corpus,
            "gap":                  p.gap,
            "months_to_goal":       p.months_to_goal,
            "on_track":             p.on_track,
            "current_monthly_sip":  p.current_monthly_sip,
        }
        for p in projections
    ]


@router.get("/recommendations")
def recommendations(
    db:       Annotated[Session, Depends(get_db)],
    owner_id: str = "family",
):
    recs = gen_recs(db, owner_id)
    return [
        {"priority": r.priority, "category": r.category,
         "title": r.title, "detail": r.detail, "action": r.action}
        for r in recs
    ]


@router.post("/refresh-corpus")
def refresh_corpus(
    db:       Annotated[Session, Depends(get_db)],
    owner_id: str = "family",
):
    """Auto-set goal current_corpus from the owner's latest net worth snapshot."""
    updated = refresh_corpus_from_networth(db, owner_id)
    return {"updated": updated, "owner_id": owner_id}


@router.get("/ai-insights")
def ai_insights(db: Annotated[Session, Depends(get_db)]):
    """
    Get AI-powered financial insights from local Ollama/Llama.
    All data stays on your machine — nothing sent to external servers.
    """
    from app.engine.ai_advisor import get_ai_insights
    return get_ai_insights(db)


@router.get("/inflation")
def current_inflation():
    """Return the latest cached India CPI inflation rate."""
    from app.engine.inflation import get_india_cpi
    return {"india_cpi": get_india_cpi(), "source": "World Bank Open Data"}


# ── Helper ────────────────────────────────────────────────────────────────────
def _goal_dict(g: Goal) -> dict:
    return {
        "id":                   str(g.id),
        "owner_id":             g.owner_id,
        "name":                 g.name,
        "goal_type":            g.goal_type,
        "target_amount":        float(g.target_amount or 0),
        "target_date":          str(g.target_date) if g.target_date else None,
        "current_corpus":       float(g.current_corpus or 0),
        "monthly_sip_needed":   float(g.monthly_sip_needed or 0),
        "expected_return_rate": float(g.expected_return_rate or 12),
        "inflation_rate":       float(g.inflation_rate or 6),
        "notes":                g.notes,
    }
