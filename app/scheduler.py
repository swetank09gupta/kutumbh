"""
APScheduler background jobs.
  - Every N hours: statements + bank alerts + loan letters + rebuild (app.etl.pipeline)
  - Daily 07:10: public rules read from their official page (RBI repo rate)
  - Daily midnight: compute net-worth snapshot
  - Weekly Sunday 8am: recalculate goal projections
  - Every 10 min: watchdog — if an update is overdue (laptop asleep, Docker paused) run it now
A job missed while the machine slept runs once on wake (coalesce) if it is within its grace; the watchdog covers the rest.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings

logger = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


OVERDUE_SLACK_H = 0.5          # an update is overdue once it is this long past its interval
GRACE_DAILY_S = 23 * 3600      # a daily job missed overnight still runs when the machine wakes
GRACE_WEEKLY_S = 6 * 86400


def update_is_overdue(last: dict | None, now: datetime, interval_h: float) -> bool:
    """True when no update has been attempted for longer than its interval (+ slack). Failures count as attempts, so a
    broken mailbox is retried once per interval, not every watchdog tick."""
    if not last:
        return True
    try:
        at = datetime.fromisoformat(last["at"])
    except (KeyError, ValueError):
        return True
    return (now - at).total_seconds() > (interval_h + OVERDUE_SLACK_H) * 3600


def _watchdog_job():
    """Wake-up safety net: APScheduler only remembers the last missed run of each job and drops it past its grace; a long sleep
    would otherwise leave the data stale until the next interval. The update refreshes statements, snapshot and goals together."""
    from datetime import timezone
    from app.models.database import SessionLocal
    from app.etl import pipeline

    if pipeline.RUN_LOCK.locked():
        return
    db = SessionLocal()
    try:
        overdue = update_is_overdue(pipeline.last_run(db), datetime.now(timezone.utc), settings.email_check_interval_hours)
    finally:
        db.close()
    if overdue:
        logger.info("Watchdog: the update is overdue — running it now")
        _sync_job()


def _sync_job():
    logger.info("Scheduled sync: starting")
    from app.models.database import SessionLocal
    from app.etl import pipeline

    db = SessionLocal()
    try:
        result = pipeline.scheduled_run(db)       # incremental — since the last successful run (at least a week)
        if result.get("skipped"):
            return
        pipeline.record_run(db, "scheduler", True)
        logger.info("Scheduled sync done: %s", result["statements"])
    except Exception as exc:
        logger.error("Scheduled sync failed: %s", exc, exc_info=True)
        db.rollback()
        pipeline.record_run(db, "scheduler", False, str(exc))
    finally:
        db.close()


def _live_rules_job():
    """Public rules read from their official page, daily and independent of e-mail (a mailbox problem must not stale them)."""
    from app.models.database import SessionLocal
    from app.knowledge import live

    db = SessionLocal()
    try:
        logger.info("Live rules: %s", live.refresh(db))
        logger.info("Rule sources re-confirmed: %s", live.verify_sources(db))
    except Exception as exc:
        logger.error("Live rules refresh failed: %s", exc, exc_info=True)
        db.rollback()
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
        try:                                                  # re-check every holding the matcher manages (sold, bought, a goal's date moved)
            from app.planning import matching
            logger.info("Holdings matching sweep: %d moved", len(matching.run(db)["moves"]))
        except Exception as exc:
            logger.warning("Holdings matching sweep failed (%s); filling gaps with the fixed rules", exc)
            db.rollback()
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


def _build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="Asia/Kolkata", job_defaults={"coalesce": True, "misfire_grace_time": GRACE_DAILY_S})

    sched.add_job(
        _sync_job,
        IntervalTrigger(hours=settings.email_check_interval_hours),
        id="email_sync",
        name="Email sync + ETL",
        replace_existing=True,
    )
    sched.add_job(
        _watchdog_job,
        IntervalTrigger(minutes=10),
        id="update_watchdog",
        name="Watchdog: run the update if overdue",
        replace_existing=True,
        max_instances=1,
    )
    sched.add_job(
        _networth_snapshot_job,
        CronTrigger(hour=0, minute=5),
        id="networth_snapshot",
        name="Daily net-worth snapshot",
        replace_existing=True,
    )
    sched.add_job(
        _holdings_autotag_job,
        CronTrigger(hour=0, minute=15),
        id="holdings_autotag",
        name="Daily holdings auto-tag sweep",
        replace_existing=True,
    )
    sched.add_job(
        _live_rules_job,
        CronTrigger(hour=7, minute=10),
        id="live_rules",
        name="Daily public-rules refresh (RBI repo rate)",
        replace_existing=True,
    )
    sched.add_job(
        _goal_update_job,
        CronTrigger(day_of_week="sun", hour=8, minute=0),
        id="goal_update",
        name="Weekly goal SIP recalculation",
        replace_existing=True,
        misfire_grace_time=GRACE_WEEKLY_S,
    )

    # The jobs above only fire while the app is up: after a restart (laptop off overnight) the day's snapshot and rule refresh are
    # skipped and the 6-hourly sync would wait a full interval. So catch up once, shortly after every start. Both are idempotent,
    # and the sync's window is "since the last successful run" (ADR-0035); its rebuild also takes the net-worth snapshot.
    now = datetime.now(sched.timezone)
    sched.add_job(_live_rules_job, DateTrigger(run_date=now + timedelta(seconds=30)), id="startup_rules",
                       name="Catch-up: public rules", replace_existing=True)
    sched.add_job(_sync_job, DateTrigger(run_date=now + timedelta(seconds=90)), id="startup_sync",
                       name="Catch-up: email sync + ETL", replace_existing=True)

    return sched


def start_scheduler():
    global _scheduler
    _scheduler = _build_scheduler()
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
