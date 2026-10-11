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
from app.engine.goals import project_all_goals, project_goal, refresh_goals, update_goal_sip, refresh_corpus_from_networth
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
    use_manual_return:    bool  = False
    sip_stepup_pct:       Optional[float] = None
    glide_enabled:        bool  = True
    glide_start_years_before: int = 3
    glide_end_return_pct: float = 8.0
    notes:                Optional[str] = None


class GoalUpdate(BaseModel):
    name:                 Optional[str]   = None
    target_amount:        Optional[float] = None
    target_date:          Optional[date]  = None
    current_corpus:       Optional[float] = None
    expected_return_rate: Optional[float] = None
    inflation_rate:       Optional[float] = None
    use_manual_return:    Optional[bool]  = None
    sip_stepup_pct:       Optional[float] = None
    glide_enabled:        Optional[bool]  = None
    glide_start_years_before: Optional[int]   = None
    glide_end_return_pct: Optional[float] = None
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
    # Cards and the projections table must tell one story: overlay the single projection.
    proj = {p.goal_id: p for p in project_all_goals(db, owner_id)}
    out = []
    for g in goals:
        d = _goal_dict(g)
        p = proj.get(str(g.id))
        if p is not None:
            d.update({
                "monthly_sip_needed":   p.monthly_sip_needed,
                "target_date":          str(p.target_date) if p.target_date else None,
                "inflation_adj_target": p.inflation_adj_target,
                "projected_corpus":     p.projected_corpus,
                "gap":                  p.gap,
                "on_track":             p.on_track,
                "current_monthly_sip":  p.current_monthly_sip,
                "effective_return_pct": p.effective_return_pct,
            })
        out.append(d)
    return out


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
        use_manual_return=body.use_manual_return,
        sip_stepup_pct=Decimal(str(body.sip_stepup_pct)) if body.sip_stepup_pct is not None else None,
        glide_enabled=body.glide_enabled,
        glide_start_years_before=body.glide_start_years_before,
        glide_end_return_pct=Decimal(str(body.glide_end_return_pct)),
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
            "target_date":          str(p.target_date) if p.target_date else None,
            "inflation_adj_target": p.inflation_adj_target,
            "current_corpus":       p.current_corpus,
            "monthly_sip_needed":   p.monthly_sip_needed,
            "projected_corpus":     p.projected_corpus,
            "gap":                  p.gap,
            "months_to_goal":       p.months_to_goal,
            "on_track":             p.on_track,
            "current_monthly_sip":  p.current_monthly_sip,
            "effective_return_pct": p.effective_return_pct,
            "return_source":        p.return_source,
            "sip_stepup_pct":       p.sip_stepup_pct,
            "sip_stepup_source":    p.sip_stepup_source,
            "glide_active":         p.glide_active,
            "blended_components":   p.blended_components,
        }
        for p in projections
    ]


@router.get("/recommendations")
def recommendations(
    db:       Annotated[Session, Depends(get_db)],
    owner_id: str = "family",
):
    recs = gen_recs(db, owner_id)
    # Persist a snapshot so we can compute month-over-month deltas later.
    # Idempotent for the same date — no duplication on repeated dashboard loads.
    try:
        from app.engine.recommendation_memory import snapshot_recommendations
        snapshot_recommendations(db, recs, owner_id=owner_id)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).debug("rec snapshot failed: %s", exc)
    return [
        {"priority": r.priority, "category": r.category,
         "title": r.title, "detail": r.detail, "action": r.action,
         "metrics": r.metrics or {}}
        for r in recs
    ]


@router.get("/recommendations/changes")
def recommendations_changes(
    db:       Annotated[Session, Depends(get_db)],
    owner_id: str = "family",
    lookback_days: int = 35,
):
    """
    Diff today's recommendations vs the most recent snapshot 7-`lookback_days`
    days old. Returns `new`, `resolved`, and `persisting` lists. Used by the
    "What's changed since last month" panel.
    """
    from app.engine.recommendation_memory import recommendation_changes
    return recommendation_changes(db, owner_id=owner_id, lookback_days=lookback_days)


@router.get("/scenarios/{goal_id}")
def goal_scenarios(
    goal_id: str,
    db:      Annotated[Session, Depends(get_db)],
):
    """
    Side-by-side projections for a single goal under four scenarios:
      base        — step-up + glide as configured
      no_stepup   — step-up forced to 0
      no_glide    — glide path disabled
      flat        — both off (textbook flat-SIP, flat-return projection)
    """
    from copy import copy
    from app.engine.goals import _scope, project_goal
    from app.engine.networth import real_owners
    from app.engine.sip import sip_stepup_pct

    goal = db.query(Goal).filter_by(id=goal_id).first()
    if not goal:
        raise HTTPException(404, "Goal not found")

    owner_ids = _scope(goal, [o.id for o in real_owners(db)])
    detected = sip_stepup_pct(db, owner_ids)
    # the scenarios vary step-up and glide path for the SIP the household really makes today
    current_sip = next((p.current_monthly_sip for p in project_all_goals(db) if p.goal_id == str(goal.id)), 0.0)

    def _variant(disable_stepup: bool, disable_glide: bool):
        g = copy(goal)
        if disable_stepup:
            g.sip_stepup_pct = 0
        if disable_glide:
            g.glide_enabled = False
        p = project_goal(g, current_sip, db, owner_ids=owner_ids,
                         detected_stepup_pct=None if disable_stepup else detected)
        return {
            "monthly_sip_needed":   p.monthly_sip_needed,
            "projected_corpus":     p.projected_corpus,
            "inflation_adj_target": p.inflation_adj_target,
            "effective_return_pct": p.effective_return_pct,
            "sip_stepup_pct":       p.sip_stepup_pct,
            "glide_active":         p.glide_active,
        }

    return {
        "goal_id":    str(goal.id),
        "name":       goal.name,
        "scenarios": {
            "base":       _variant(False, False),
            "no_stepup":  _variant(True,  False),
            "no_glide":   _variant(False, True),
            "flat":       _variant(True,  True),
        },
    }


@router.post("/refresh-corpus")
def refresh_corpus(
    db:       Annotated[Session, Depends(get_db)],
    owner_id: str = "family",
):
    """Auto-set goal current_corpus from the owner's latest net worth snapshot."""
    updated = refresh_corpus_from_networth(db, owner_id)
    refreshed = refresh_goals(db)
    return {"updated": updated, "owner_id": owner_id, **refreshed}


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


@router.get("/market-signals")
def current_market_signals():
    """
    Returns current market valuation signals (Nifty 50 / Midcap / Smallcap /
    500 P/Es, USD/INR, Nasdaq 100, India 10-yr yield) with cheap/fair/expensive
    zones vs historical averages. 12-hour cached.
    """
    from app.engine.market_signals import get_market_signals
    return get_market_signals()


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
        "use_manual_return":    bool(getattr(g, "use_manual_return", False)),
        "sip_stepup_pct":       float(g.sip_stepup_pct) if getattr(g, "sip_stepup_pct", None) is not None else None,
        "glide_enabled":        bool(getattr(g, "glide_enabled", True)),
        "glide_start_years_before": int(getattr(g, "glide_start_years_before", 3) or 3),
        "glide_end_return_pct": float(getattr(g, "glide_end_return_pct", 8) or 8),
        "notes":                g.notes,
    }
