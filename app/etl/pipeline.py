"""
The one definition of "bring everything up to date", used by the scheduler, the Sync button and reprocess, so what runs
by itself is exactly what runs when you press the button.

    statements (fetch + parse)  →  ingest_evidence  →  rebuild  →  record_run

`ingest_evidence`: bank alert e-mails, HDFC loan letters, public rules read from their official page. Each step is
isolated: one failing never blocks the others or the statements.
`rebuild`: maintenance → ledger → net-worth snapshot → goals → consistency, in that order (each reads the one before).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

RUN_KEY = "pipeline.last_run"
RUN_LOCK = threading.Lock()      # one update at a time: the scheduler and the Sync button never overlap


def ingest_evidence(db: Session, since_days: int = 7) -> dict:
    from app.ingestion.alert_fetcher import ingest_alerts
    from app.ingestion.loan_fetcher import ingest_loan_letters
    from app.knowledge import live
    from app.ledger import dividends

    steps = (("alerts", lambda: ingest_alerts(db, since_days=max(since_days, 7))),
             ("loan_letters", lambda: ingest_loan_letters(db, since_days=max(since_days, 7))),
             ("rules", lambda: live.refresh(db)),
             ("dividend_history", lambda: dividends.refresh_history(db)))
    out: dict = {}
    for name, fn in steps:
        try:
            out[name] = fn()
        except Exception as exc:                          # evidence must never block the statement pipeline
            logger.error("%s ingestion failed: %s", name, exc, exc_info=True)
            db.rollback()
            out[name] = {"error": str(exc)}
    return out


def rebuild(db: Session) -> dict:
    from app import consistency
    from app.engine.goals import refresh_goals
    from app.engine.networth import snapshot_all
    from app.etl.maintenance import run_all as run_maintenance
    from app.ledger import refresh as ledger_refresh

    maintenance = run_maintenance(db)          # clean, de-dupe, tag first…
    ledger = ledger_refresh(db)                # …then rebuild holdings…
    snapshots = snapshot_all(db)               # …then snapshot net worth from them…
    goals = refresh_goals(db)                  # …then goals from the same holdings…
    health = consistency.run(db)               # …and verify every page will agree
    return {"maintenance": maintenance, "ledger": ledger, "snapshots": snapshots, "goals": goals, "health": health}


def record_run(db: Session, source: str, ok: bool, detail: str = "") -> None:
    """Remember when the update last finished, so a scheduler that silently stopped shows up (consistency check)."""
    from app.models.schemas import AppSetting
    value = json.dumps({"at": datetime.now(timezone.utc).isoformat(), "source": source, "ok": ok, "detail": detail[:120]})
    row = db.get(AppSetting, RUN_KEY)
    if row is None:
        db.add(AppSetting(key=RUN_KEY, value=value))
    else:
        row.value = value
    db.commit()


def last_run(db: Session) -> dict | None:
    from app.models.schemas import AppSetting
    row = db.get(AppSetting, RUN_KEY)
    try:
        return json.loads(row.value) if row else None
    except ValueError:
        return None


def scheduled_run(db: Session, since_days: int = 7) -> dict:
    """What the 6-hourly job does: new statements, then the same evidence + rebuild as the Sync button."""
    if not RUN_LOCK.acquire(blocking=False):
        logger.warning("Scheduled update skipped: another update is still running")
        return {"skipped": True}
    try:
        return _scheduled_run(db, since_days)
    finally:
        RUN_LOCK.release()


def _scheduled_run(db: Session, since_days: int) -> dict:
    from app.ingestion.attachment_router import process_attachments
    from app.ingestion.email_fetcher import fetch_all_accounts

    summary = process_attachments(fetch_all_accounts(since_days=since_days), db)
    evidence = ingest_evidence(db, since_days)
    built = rebuild(db)
    return {"statements": summary, "evidence": evidence, **built}
