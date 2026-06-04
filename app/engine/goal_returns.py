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

    components: list[dict] = []
    total_value = 0.0
    cap_applied = False

    # ── Holdings tagged to this goal ────────────────────────────────────────
    if goal_type:
        instruments = _per_instrument_returns(db, owner_ids, today)
        for inst in instruments:
            if (inst.get("goal_tag") or "") != goal_type:
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
    if goal_type:
        manual_assets = (
            db.query(ManualAsset)
            .filter(ManualAsset.owner_id.in_(owner_ids))
            .filter(ManualAsset.goal_tag == goal_type)
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


def detect_sip_stepup(
    db: Session, owner_ids: list[str], today: Optional[date] = None,
) -> Optional[float]:
    """
    Compare monthly investing rate in months [-23..-12] vs [-11..0].
    Return annualized growth % between the two windows, or None if insufficient
    data (e.g. <12 months of history, or either window is zero).
    """
    today = today or date.today()
    end_recent = today
    start_recent = today - timedelta(days=int(30.44 * 12))
    start_prior  = today - timedelta(days=int(30.44 * 24))

    def _window_sum(d_from: date, d_to: date) -> float:
        inv = (
            db.query(_f.coalesce(_f.sum(Transaction.amount), 0))
            .join(Account, Transaction.account_id == Account.id)
            .filter(
                Account.owner_id.in_(owner_ids),
                Account.account_type.in_(("stocks", "mutual_fund", "demat")),
                Transaction.transaction_type == "debit",
                Transaction.date >= d_from,
                Transaction.date <  d_to,
                ~Transaction.description.ilike("DEMAT %"),
            )
            .scalar()
        ) or 0
        # Bank-account ACH debits to broker mandates (SIPs visible on bank stmt
        # only, e.g. when Coin emails aren't ingested for that owner).
        broker_ach = (
            db.query(_f.coalesce(_f.sum(Transaction.amount), 0))
            .join(Account, Transaction.account_id == Account.id)
            .filter(
                Account.owner_id.in_(owner_ids),
                Account.account_type == "bank",
                Transaction.transaction_type == "debit",
                Transaction.date >= d_from,
                Transaction.date <  d_to,
                (
                    Transaction.description.ilike("%YESB00709000028661%")
                    | Transaction.description.ilike("%ZERODHA BROKIN%")
                    | Transaction.description.ilike("%INDIAN CLEARIN%")
                ),
            )
            .scalar()
        ) or 0
        inv = float(inv) + float(broker_ach)
        contribs = (
            db.query(_f.coalesce(_f.sum(ManualAssetContribution.amount), 0))
            .join(ManualAsset, ManualAsset.id == ManualAssetContribution.asset_id)
            .filter(
                ManualAsset.owner_id.in_(owner_ids),
                ManualAssetContribution.date >= d_from,
                ManualAssetContribution.date <  d_to,
            )
            .scalar()
        ) or 0
        return float(inv) + float(contribs)

    recent_total = _window_sum(start_recent, end_recent)
    prior_total  = _window_sum(start_prior, start_recent)

    if prior_total <= 0 or recent_total <= 0:
        return None
    # Both windows are 12 months wide, so the ratio is already annual.
    growth = (recent_total / prior_total) - 1.0
    # Clamp to a sane long-run salary-growth range. A one-off 60% jump
    # (e.g. recently started a new SIP) shouldn't extrapolate forever.
    growth_pct = max(0.0, min(12.0, growth * 100.0))
    return round(growth_pct, 1)


def actual_monthly_sip_for_goal(
    db: Session, goal, owner_ids: list[str],
    lookback_months: int = 6, today: Optional[date] = None,
) -> float:
    """
    Real monthly contribution flowing into this goal, computed from:
      - Transactions whose ISIN maps to a holding tagged with this goal_type,
      - Plus contributions to ManualAssets tagged with this goal_type.

    Falls back to 0 for goals without tagged holdings/assets.
    """
    today = today or date.today()
    goal_type = (goal.goal_type or "").strip()
    if not goal_type:
        return 0.0
    cutoff = today - timedelta(days=int(30.44 * lookback_months))

    # ISINs of holdings tagged to this goal for these owners (Holding → Account.owner_id)
    tagged_isins = {
        row[0] for row in
        db.query(Holding.isin)
          .join(Account, Account.id == Holding.account_id)
          .filter(Account.owner_id.in_(owner_ids))
          .filter(Holding.goal_tag == goal_type)
          .filter(Holding.isin.isnot(None))
          .all()
    }

    inv_sum = 0.0
    if tagged_isins:
        inv_sum = float(
            db.query(_f.coalesce(_f.sum(Transaction.amount), 0))
            .join(Account, Transaction.account_id == Account.id)
            .filter(
                Account.owner_id.in_(owner_ids),
                Transaction.transaction_type == "debit",
                Transaction.date >= cutoff,
                Transaction.isin.in_(tagged_isins),
                ~Transaction.description.ilike("DEMAT %"),
            )
            .scalar() or 0
        )

    # Manual asset contributions for assets tagged to this goal
    contribs = float(
        db.query(_f.coalesce(_f.sum(ManualAssetContribution.amount), 0))
        .join(ManualAsset, ManualAsset.id == ManualAssetContribution.asset_id)
        .filter(
            ManualAsset.owner_id.in_(owner_ids),
            ManualAsset.goal_tag == goal_type,
            ManualAssetContribution.date >= cutoff,
        )
        .scalar() or 0
    )

    months = max(1, lookback_months)
    return (inv_sum + contribs) / months
