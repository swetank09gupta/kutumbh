"""
Per-goal blended return + SIP step-up detection + per-goal actual SIP.

Used by `goals.project_goal` to produce realistic projections that:
  - reflect each holding's actual historical XIRR (capped by category benchmark),
  - assume a configurable SIP step-up (or auto-detect from recent history),
  - measure the user's *real* monthly contribution to this goal (not a pooled split).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func as _f
from sqlalchemy.orm import Session

from app.models.schemas import (
    Account, Holding, ManualAsset, ManualAssetContribution, Transaction,
)

# ── Long-run benchmark caps (used to dampen unrealistically high recent XIRRs) ─
# Tuned for India long-horizon planning. Conservative side of consensus.
CATEGORY_CAPS_PCT: dict[str, float] = {
    "equity":       13.5,
    "mutual_fund":  13.0,
    "bonds":         8.0,
    "fd":            7.0,
    "rd":            6.5,
    "ppf":           7.5,
    "nps":          10.0,
    "gold":          9.0,
    "silver":        9.0,
    "sgb":           9.0,
    "real_estate":   7.0,
    "insurance":     6.0,
    "demat":        12.0,
    "other":         8.0,
}

# Default fallback return when a goal has no tagged holdings and no manual override.
DEFAULT_RETURN_PCT = 12.0


def goal_tags(goal) -> list[str]:
    """
    The holding goal_tag(s) that fund a goal. A family retirement goal draws on both retirement tags (retirement and
    wife_retirement); a personal one on its own; every other goal uses its type.
    One definition for corpus, blended return and current SIP, so they cannot drift apart.
    """
    gt = (goal.goal_type or "").strip()
    owner = (goal.owner_id or "").lower()
    if gt == "retirement":
        if owner in ("", "family"):
            return ["retirement", "wife_retirement"]                    # one household retirement draws on everything tagged for it
        if owner in ("wife", "spouse", "partner") or "wife" in (goal.name or "").lower():
            return ["wife_retirement"]
        return ["retirement"]
    return [gt] if gt else []


def _cap_for(htype: Optional[str]) -> float:
    return CATEGORY_CAPS_PCT.get((htype or "other").lower(), CATEGORY_CAPS_PCT["other"])


def compute_blended_return(
    db: Session, goal, owner_ids: list[str], today: Optional[date] = None,
) -> dict:
    """
    Value-weighted blended expected return for a goal, computed from the
    historical XIRR / CAGR of holdings + manual assets tagged to this goal.

    Each holding's return is capped at its category benchmark before blending,
    so a fund that ran 28% in a 3-yr bull market doesn't extrapolate at 28%
    forever.

    Returns dict:
      blended_pct: float | None     # None if no tagged components found
      components:  list[dict]       # [{name, type, value, raw_pct, capped_pct}]
      cap_applied: bool             # True if any component was capped
      total_value: float
    """
    from app.engine.portfolio_xirr import _per_instrument_returns

    today = today or date.today()
    goal_type = (goal.goal_type or "").strip()
    tags = goal_tags(goal)

    components: list[dict] = []
    total_value = 0.0
    cap_applied = False

    # ── Holdings tagged to this goal ────────────────────────────────────────
    if tags:
        instruments = _per_instrument_returns(db, owner_ids, today)
        for inst in instruments:
            if (inst.get("goal_tag") or "") not in tags:
                continue
            value = float(inst.get("current") or 0)
            if value <= 0:
                continue
            raw = inst.get("xirr_pct")
            if raw is None:
                raw = inst.get("cagr_pct")
            if raw is None:
                continue
            cap = _cap_for(inst.get("type"))
            capped = min(float(raw), cap)
            if capped < float(raw):
                cap_applied = True
            components.append({
                "name":       inst.get("name"),
                "type":       inst.get("type"),
                "value":      value,
                "raw_pct":    float(raw),
                "capped_pct": capped,
            })
            total_value += value

    # ── Manual assets tagged to this goal ────────────────────────────────────
    if tags:
        manual_assets = (
            db.query(ManualAsset)
            .filter(ManualAsset.owner_id.in_(owner_ids))
            .filter(ManualAsset.goal_tag.in_(tags))
            .all()
        )
        for a in manual_assets:
            value = float(a.current_value or 0)
            if value <= 0:
                continue
            invested = float(a.invested_amount or 0)
            if invested <= 0 or not a.start_date:
                continue
            years = max(0.01, (today - a.start_date).days / 365.25)
            try:
                raw = (pow(value / invested, 1.0 / years) - 1.0) * 100
            except (ValueError, ZeroDivisionError):
                continue
            cap = _cap_for(a.asset_type)
            capped = min(raw, cap)
            if capped < raw:
                cap_applied = True
            components.append({
                "name":       a.name,
                "type":       a.asset_type,
                "value":      value,
                "raw_pct":    raw,
                "capped_pct": capped,
            })
            total_value += value

    if total_value <= 0 or not components:
        return {
            "blended_pct": None,
            "components":  [],
            "cap_applied": False,
            "total_value": 0.0,
        }

    blended = sum(c["capped_pct"] * c["value"] for c in components) / total_value
    # Holdings can show transient negative XIRR right after a purchase (current
    # value briefly below invested). Don't extrapolate that as the long-run
    # return — fall back to the conservative default when the blend looks
    # implausibly low for a multi-decade projection.
    if blended < 4.0:
        return {
            "blended_pct": None,
            "components":  components,
            "cap_applied": cap_applied,
            "total_value": round(total_value, 2),
            "floor_applied": True,
        }
    return {
        "blended_pct": round(blended, 2),
        "components":  components,
        "cap_applied": cap_applied,
        "total_value": round(total_value, 2),
    }
