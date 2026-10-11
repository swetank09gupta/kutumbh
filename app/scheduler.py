"""
APScheduler background jobs.
  - Every N hours: fetch emails + ETL
  - Daily midnight: compute net-worth snapshot
  - Weekly Sunday 8am: recalculate goal projections
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings

logger = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def _sync_job():
    logger.info("Scheduled sync: starting")
    from app.models.database import SessionLocal
    from app.ingestion.email_fetcher import fetch_all_accounts
    from app.ingestion.attachment_router import process_attachments
    from app.engine.networth import snapshot_all

    db = SessionLocal()
    try:
        attachments = fetch_all_accounts(since_days=7)  # incremental — last 7 days
        summary     = process_attachments(attachments, db)
        from app.ledger import refresh as ledger_refresh
        from app.etl.maintenance import run_all as run_maintenance
        run_maintenance(db)              # de-dupe, own-transfer pairing, tagging — same as a manual sync
        ledger_refresh(db)
        snapshot_all(db)
        from app.engine.goals import refresh_goals
        refresh_goals(db)
        logger.info("Scheduled sync done: %s", summary)
    except Exception as exc:
        logger.error("Scheduled sync failed: %s", exc, exc_info=True)
    finally:
        db.close()


def _networth_snapshot_job():
    logger.info("Daily net-worth snapshot: starting")
    from app.models.database import SessionLocal
    from app.engine.networth import snapshot_all

    db = SessionLocal()
    try:
        from app.ledger import refresh as ledger_refresh
        ledger_refresh(db)          # re-price today's positions before snapshotting
        snapshot_all(db)
        from app.engine.goals import refresh_goals
        refresh_goals(db)
    except Exception as exc:
        logger.error("Net-worth snapshot failed: %s", exc, exc_info=True)
    finally:
        db.close()


def _holdings_autotag_job():
    """Daily sweep — tag any holdings that slipped through without a goal_tag,
    then refresh every goal's current_corpus from those tags."""
    logger.info("Holdings auto-tag sweep: starting")
    from app.models.database import SessionLocal
    from app.engine.holding_tagger import auto_tag_holdings
    from app.engine.goals import refresh_corpus_from_networth

    db = SessionLocal()
    try:
        n = auto_tag_holdings(db)
        logger.info("Holdings auto-tag sweep: tagged %d", n)
        # Always refresh corpus, even if no new tags — net worth / holdings
        # values change daily and the goal page should reflect that.
        from app.engine.goals import refresh_goals
        logger.info("Goal refresh: %s", refresh_goals(db))
    except Exception as exc:
        logger.error("Holdings auto-tag/corpus refresh failed: %s", exc, exc_info=True)
    finally:
        db.close()


def _goal_update_job():
    logger.info("Weekly goal SIP recalculation: starting")
    from app.models.database import SessionLocal
    from app.engine.goals import refresh_goals

    db = SessionLocal()
    try:
        logger.info("Goal refresh: %s", refresh_goals(db))
    except Exception as exc:
        logger.error("Goal update failed: %s", exc, exc_info=True)
    finally:
        db.close()


def start_scheduler():
    global _scheduler
    _scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

    _scheduler.add_job(
        _sync_job,
        IntervalTrigger(hours=settings.email_check_interval_hours),
        id="email_sync",
        name="Email sync + ETL",
        replace_existing=True,
        misfire_grace_time=300,
    )
    _scheduler.add_job(
        _networth_snapshot_job,
        CronTrigger(hour=0, minute=5),
        id="networth_snapshot",
        name="Daily net-worth snapshot",
        replace_existing=True,
    )
    _scheduler.add_job(
        _holdings_autotag_job,
        CronTrigger(hour=0, minute=15),
        id="holdings_autotag",
        name="Daily holdings auto-tag sweep",
        replace_existing=True,
    )
    _scheduler.add_job(
        _goal_update_job,
        CronTrigger(day_of_week="sun", hour=8, minute=0),
        id="goal_update",
        name="Weekly goal SIP recalculation",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info(
        "Scheduler started. Email sync every %dh, net-worth snapshot daily 00:05 IST.",
        settings.email_check_interval_hours,
    )


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")
