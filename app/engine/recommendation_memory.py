"""
Recommendation memory — persists rec snapshots and computes lifecycle deltas.

Used by the dashboard to show:
  * 🆕 New — recs that appeared in the last N days
  * 🔄 Persisting — recs that have been around for >=3 consecutive monthly snapshots
  * ✅ Resolved — recs that were active before but have dropped off
  * 📈 Improving / 📉 Worsening — metric trend over time (e.g. midcap_pct 28→26→24)

Snapshot cadence: at most one snapshot per (fingerprint, snapshot_date). The
default `snapshot_date` is today, so multiple dashboard loads in a day don't
write multiple rows.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date, timedelta
from typing import Iterable

from sqlalchemy.orm import Session

from app.engine.recommendations import Recommendation
from app.models.schemas import RecommendationSnapshot

logger = logging.getLogger(__name__)


def snapshot_recommendations(
    db: Session, recs: Iterable[Recommendation], owner_id: str = "family",
    snapshot_date: date | None = None,
) -> int:
    """
    Persist today's recommendations. Idempotent for the same (fingerprint,
    snapshot_date) — only the first call of the day writes; subsequent calls
    update the row in place so the latest metrics win.
    """
    snapshot_date = snapshot_date or date.today()
    n_written = 0
    for r in recs:
        existing = (
            db.query(RecommendationSnapshot)
            .filter_by(
                snapshot_date=snapshot_date,
                owner_id=owner_id,
                fingerprint=r.fingerprint,
            )
            .first()
        )
        metrics_json = json.dumps(r.metrics or {}, default=str)
        if existing:
            existing.priority = r.priority
            existing.detail   = r.detail
            existing.action   = r.action
            existing.metrics  = metrics_json
        else:
            db.add(RecommendationSnapshot(
                snapshot_date=snapshot_date,
                owner_id=owner_id,
                fingerprint=r.fingerprint,
                priority=r.priority,
                category=r.category,
                title=r.title,
                detail=r.detail,
                action=r.action,
                metrics=metrics_json,
            ))
            n_written += 1
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("snapshot commit failed: %s", exc)
        return 0
    return n_written


def recommendation_changes(
    db: Session, owner_id: str = "family",
    today: date | None = None,
    lookback_days: int = 35,
) -> dict:
    """
    Diff today's active recs vs the most-recent prior snapshot (>= 7 days ago,
    <= `lookback_days` ago).

    Returns:
      {
        "today": "2026-06-04",
        "compared_to": "2026-05-04",
        "new":        [Recommendation-like dict, …]   # active today, absent before
        "resolved":   [Recommendation-like dict, …]   # was active before, absent today
        "persisting": [{rec, since_date, count, metric_trend}]  # active both times
      }
    """
    today = today or date.today()
    older_window_start = today - timedelta(days=lookback_days)
    older_window_end   = today - timedelta(days=7)

    today_rows = (
        db.query(RecommendationSnapshot)
        .filter_by(owner_id=owner_id, snapshot_date=today)
        .all()
    )
    today_map = {r.fingerprint: r for r in today_rows}

    # Pick the latest snapshot before today within window
    prior_dates = (
        db.query(RecommendationSnapshot.snapshot_date)
        .filter(RecommendationSnapshot.owner_id == owner_id)
        .filter(RecommendationSnapshot.snapshot_date >= older_window_start)
        .filter(RecommendationSnapshot.snapshot_date <= older_window_end)
        .distinct()
        .order_by(RecommendationSnapshot.snapshot_date.desc())
        .all()
    )
    prior_date = prior_dates[0][0] if prior_dates else None
    prior_map: dict[str, RecommendationSnapshot] = {}
    if prior_date:
        prior_rows = (
            db.query(RecommendationSnapshot)
            .filter_by(owner_id=owner_id, snapshot_date=prior_date)
            .all()
        )
        prior_map = {r.fingerprint: r for r in prior_rows}

    def _to_dict(r: RecommendationSnapshot) -> dict:
        try:
            metrics = json.loads(r.metrics) if r.metrics else {}
        except Exception:
            metrics = {}
        return {
            "fingerprint": r.fingerprint,
            "priority":    r.priority,
            "category":    r.category,
            "title":       r.title,
            "detail":      r.detail,
            "action":      r.action,
            "metrics":     metrics,
            "snapshot_date": str(r.snapshot_date),
        }

    new       = [_to_dict(r) for fp, r in today_map.items() if fp not in prior_map]
    resolved  = [_to_dict(r) for fp, r in prior_map.items() if fp not in today_map]

    persisting = []
    for fp, r_today in today_map.items():
        if fp not in prior_map:
            continue
        # Trace history: how many distinct dates this fingerprint has appeared
        # over the lookback window, and earliest date.
        history = (
            db.query(RecommendationSnapshot.snapshot_date,
                     RecommendationSnapshot.metrics)
            .filter(RecommendationSnapshot.owner_id == owner_id)
            .filter(RecommendationSnapshot.fingerprint == fp)
            .filter(RecommendationSnapshot.snapshot_date >= older_window_start)
            .order_by(RecommendationSnapshot.snapshot_date)
            .all()
        )
        dates = [h[0] for h in history]
        # Build a trend for the primary numeric metric (first non-string value)
        trend: list[dict] = []
        for h in history:
            try:
                m = json.loads(h[1]) if h[1] else {}
            except Exception:
                m = {}
            primary = next(((k, v) for k, v in m.items()
                            if isinstance(v, (int, float)) and not isinstance(v, bool)),
                           None)
            if primary:
                trend.append({"date": str(h[0]), "metric": primary[0], "value": primary[1]})
        d = _to_dict(r_today)
        d["since_date"]   = str(dates[0]) if dates else None
        d["count_snapshots"] = len(dates)
        d["trend"]        = trend
        persisting.append(d)

    return {
        "today":        str(today),
        "compared_to":  str(prior_date) if prior_date else None,
        "new":          new,
        "resolved":     resolved,
        "persisting":   persisting,
    }
