"""
What keeps the app current without anyone: public rules read from their official page (laid over rules.yml),
the one shared update pipeline (scheduler = Sync button), and the check that notices when it stops.
"""
import inspect
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from app.knowledge import live, rules

URL = os.getenv("TEST_DATABASE_URL")
APP = Path(__file__).resolve().parents[1] / "app"
YAML_REPO = rules.rule("rbi.repo_rate")          # 5.50 from 2026-10-07, verified 2026-10-12
N_FILE = len(rules.load()["rbi.repo_rate"])


def _reset():
    rules.set_observations([])


def _sample(regex: str) -> str:
    """Text that a probe regex matches: its first alternative with the regex syntax made literal."""
    first = regex.split("|")[0]
    for a, b in (("\\.", "."), ("\\d+", "1"), (" ?", " "), ("?", ""), ("(%", "%"), ("[.,]", "."), ("(complete )", ""), ("(financial )", ""), ("[- ]", "-"),
                 ("(", ""), (")", ""), ("\\", "")):
        first = first.replace(a, b)
    return first


class ParseRepo(unittest.TestCase):
    def test_reads_the_rate_from_the_current_rates_box(self):
        html = "<div>Policy&nbsp; Rates</div><p>Policy Repo Rate : <b>5.50%</b></p><p>Standing Deposit Facility Rate : 5.25%</p>"
        self.assertEqual(live.parse_repo(html), D("5.50"))

    def test_the_text_variant_of_the_page(self):
        self.assertEqual(live.parse_repo("Rates Policy Repo Rate : 5.75% Standing Deposit Facility Rate : 5.50%"), D("5.75"))

    def test_no_value_when_the_page_has_changed_or_is_implausible(self):
        self.assertIsNone(live.parse_repo("<html>Maintenance</html>"))
        self.assertIsNone(live.parse_repo("Repo Rate : 55.00%"))
        self.assertIsNone(live.parse_repo("Repo Rate : 0.00%"))


class Overlay(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_reset)
        _reset()

    def test_an_observation_that_agrees_only_refreshes_verified_on(self):
        rules.set_observations([("rbi.repo_rate", "5.50", date(2026, 12, 1), date(2027, 1, 5), live.RBI_HOME)])
        entries = rules.load()["rbi.repo_rate"]
        self.assertEqual(len(entries), N_FILE)                                      # nothing added
        self.assertEqual(entries[-1].verified_on, date(2027, 1, 5))
        self.assertEqual(entries[-1].effective_from, YAML_REPO.effective_from)     # the file's date is kept

    def test_a_changed_value_starts_a_new_entry_and_keeps_the_old_one(self):
        rules.set_observations([("rbi.repo_rate", "5.75", date(2026, 12, 5), date(2026, 12, 20), live.RBI_HOME)])
        self.assertEqual(rules.value("rbi.repo_rate", date(2026, 12, 4)), D("5.50"))      # history stays right
        self.assertEqual(rules.value("rbi.repo_rate", date(2026, 12, 5)), D("5.75"))
        self.assertEqual(rules.value("rbi.repo_rate", date(2026, 12, 25)), D("5.75"))
        self.assertIn("automatically", rules.rule("rbi.repo_rate", date(2026, 12, 25)).note)

    def test_a_later_change_back_is_a_third_entry(self):
        rules.set_observations([("rbi.repo_rate", "5.75", date(2026, 12, 5), date(2027, 2, 1), live.RBI_HOME),
                                ("rbi.repo_rate", "5.50", date(2027, 2, 5), date(2027, 2, 6), live.RBI_HOME)])
        self.assertEqual([r.value for r in rules.load()["rbi.repo_rate"]][-3:], [D("5.50"), D("5.75"), D("5.50")])
        self.assertEqual(rules.value("rbi.repo_rate", date(2027, 1, 1)), D("5.75"))
        self.assertEqual(rules.value("rbi.repo_rate", date(2027, 3, 1)), D("5.50"))

    def test_once_the_file_knows_the_change_the_observation_adds_nothing(self):
        base = {"x.rate": (rules.Rule("x.rate", D("6"), "%", date(2026, 1, 1), "u", date(2026, 1, 1)),
                           rules.Rule("x.rate", D("7"), "%", date(2026, 12, 4), "u", date(2026, 12, 6)))}
        with mock.patch.object(rules, "_from_file", return_value=base):
            rules.set_observations([("x.rate", "7", date(2026, 12, 5), date(2026, 12, 8), "u")])
            entries = rules.load()["x.rate"]
        self.assertEqual([r.effective_from for r in entries], [date(2026, 1, 1), date(2026, 12, 4)])   # file's true date wins
        self.assertEqual(entries[1].verified_on, date(2026, 12, 8))

    def test_an_observation_never_invents_a_rule(self):
        rules.set_observations([("made.up", "1", date(2026, 1, 1), date(2026, 1, 1), "u")])
        self.assertNotIn("made.up", rules.load())

    def test_a_fresh_observation_clears_that_rules_staleness_only(self):
        quarter_on = date(2027, 1, 5)
        before = {r["id"] for r in rules.stale(quarter_on)}
        rules.set_observations([("rbi.repo_rate", "5.50", date(2027, 1, 5), date(2027, 1, 5), live.RBI_HOME)])
        after = {r["id"] for r in rules.stale(quarter_on)}
        self.assertIn("rbi.repo_rate", before)
        self.assertEqual(before - after, {"rbi.repo_rate"})


class SelfUpdatingRulesMustKeepUpdating(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_reset)
        _reset()

    def test_a_silent_failure_is_reported_within_days_not_at_the_next_quarter(self):
        fresh = {"rbi.repo_rate": live.MAX_AGE_DAYS}
        day = date(2026, 11, 20)                                        # same quarter: the by-hand rules are still fine
        self.assertEqual(rules.stale(day), [])                          # the old logic sees nothing wrong
        stuck = rules.stale(day, fresh_within=fresh)
        self.assertEqual([r["id"] for r in stuck], ["rbi.repo_rate"])
        self.assertIn("updates itself", stuck[0]["why"])

    def test_a_recent_read_clears_it(self):
        rules.set_observations([("rbi.repo_rate", "5.50", date(2026, 11, 19), date(2026, 11, 19), live.RBI_HOME)])
        self.assertEqual(rules.stale(date(2026, 11, 20), fresh_within={"rbi.repo_rate": live.MAX_AGE_DAYS}), [])

    def test_the_consistency_check_names_the_stuck_rule(self):
        from app import consistency
        with mock.patch("app.knowledge.rules.stale", return_value=[
                {"id": "rbi.repo_rate", "why": "updates itself but has not been read for 9 days"}]):
            c = consistency.rules_are_current(None)
        self.assertFalse(c["ok"])
        self.assertIn("should update itself", c["detail"])
        self.assertIn("rbi.repo_rate", c["detail"])


class SourceProbes(unittest.TestCase):
    """Rules whose source page can be re-read confirm themselves daily; one whose page stops confirming warns within days."""
    def setUp(self):
        self.addCleanup(_reset)
        _reset()

    def test_probed_rules_exist_and_every_probe_is_a_valid_regex(self):
        import re
        probed = live.probed_rule_ids()
        self.assertGreaterEqual(len(probed), 12)
        for rid in probed:
            re.compile(rules.rule(rid).probe)

    def test_a_probe_is_only_on_the_entry_in_force_and_its_page_is_a_url(self):
        for rid, entries in rules.load().items():
            for e in entries[:-1]:
                self.assertEqual(e.probe, "", f"{rid}: an old entry must not be probed")
            for e in entries:
                if e.probe:
                    self.assertRegex(e.probe_url or e.source, r"^https?://")

    def test_the_stale_check_names_a_probed_rule_whose_page_stopped_confirming(self):
        fresh = {rid: live.PROBE_MAX_AGE_DAYS for rid in live.probed_rule_ids()}
        day = date(2026, 12, 1)                                                  # 50 days after verification, same quarter-ish
        stuck = {r["id"] for r in rules.stale(day, fresh_within=fresh)}
        self.assertIn("ppf.annual_limit", stuck)
        self.assertTrue(all("updates itself" in r["why"] for r in rules.stale(day, fresh_within=fresh) if r["id"] in fresh))


class CatchUpWindow(unittest.TestCase):
    """The update looks back to the last successful run, so a sleeping laptop or a stopped container never leaves a permanent hole."""
    def window(self, last, today=date(2026, 10, 12)):
        from app.etl import pipeline
        with mock.patch.object(pipeline, "last_run", return_value=last):
            return pipeline.catch_up_window(mock.Mock(), today)

    def at(self, days_ago, ok=True):
        return {"at": (datetime(2026, 10, 12, 12, tzinfo=timezone.utc) - timedelta(days=days_ago)).isoformat(), "ok": ok}

    def test_never_run_looks_back_a_long_way(self):
        self.assertEqual(self.window(None), 400)

    def test_a_recent_run_still_looks_back_a_week(self):
        self.assertEqual(self.window(self.at(0)), 7)
        self.assertEqual(self.window(self.at(2)), 7)

    def test_a_gap_is_covered_with_an_overlap(self):
        self.assertEqual(self.window(self.at(20)), 23)

    def test_a_very_old_run_is_capped(self):
        self.assertEqual(self.window(self.at(900)), 400)

    def test_a_failed_last_run_looks_back_a_month(self):
        self.assertEqual(self.window(self.at(1, ok=False)), 30)

    def test_the_scheduled_run_asks_for_the_window_after_taking_the_lock(self):
        import inspect
        from app.etl import pipeline
        self.assertIn("catch_up_window(db)", inspect.getsource(pipeline._scheduled_run))
        self.assertNotIn("since_days=7", inspect.getsource(__import__("app.scheduler", fromlist=["x"])._sync_job))


class OneSharedPipeline(unittest.TestCase):
    def test_rebuild_runs_in_dependency_order(self):
        from app.etl import pipeline
        calls = []
        with mock.patch("app.txn.alert_recon.reconcile_alerts", lambda db: calls.append("alerts")), \
             mock.patch("app.etl.maintenance.run_all", lambda db: calls.append("maintenance")), \
             mock.patch("app.ledger.refresh", lambda db: calls.append("ledger")), \
             mock.patch("app.engine.networth.snapshot_all", lambda db: calls.append("snapshot") or []), \
             mock.patch("app.engine.goals.refresh_goals", lambda db: calls.append("goals")), \
             mock.patch("app.consistency.run", lambda db: calls.append("consistency") or {}):
            pipeline.rebuild(object())
        self.assertEqual(calls, ["alerts", "maintenance", "ledger", "snapshot", "goals", "consistency"])

    def test_one_failing_evidence_source_does_not_stop_the_others(self):
        from app.etl import pipeline
        db = mock.Mock()
        with mock.patch("app.ingestion.alert_fetcher.ingest_alerts", side_effect=RuntimeError("imap down")), \
             mock.patch("app.ingestion.loan_fetcher.ingest_loan_certificates", return_value={"new": 0}), \
             mock.patch("app.ingestion.registrar_fetcher.ingest_registrar_notices", return_value={"notices": 0}), \
             mock.patch("app.ingestion.loan_fetcher.ingest_loan_letters", return_value={"new": 1}) as loans, \
             mock.patch("app.ledger.dividends.refresh_history", return_value={"fetched": 0}), \
             mock.patch("app.knowledge.live.verify_sources", return_value={}), \
             mock.patch("app.knowledge.live.refresh", return_value={"rbi.repo_rate": "unchanged"}) as rule_refresh:
            out = pipeline.ingest_evidence(db, 7)
        self.assertEqual(out["alerts"], {"error": "imap down"})
        self.assertEqual(out["loan_letters"], {"new": 1})
        self.assertEqual(out["rules"], {"rbi.repo_rate": "unchanged"})
        loans.assert_called_once(); rule_refresh.assert_called_once(); db.rollback.assert_called()

    def test_scheduled_run_skips_when_an_update_is_already_running(self):
        from app.etl import pipeline
        self.assertTrue(pipeline.RUN_LOCK.acquire(blocking=False))
        try:
            self.assertEqual(pipeline.scheduled_run(mock.Mock()), {"skipped": True})
        finally:
            pipeline.RUN_LOCK.release()

    def test_after_a_restart_the_rules_and_the_sync_catch_up_once(self):
        """A laptop that was off at 00:05 / 07:10 must not leave the day without a snapshot or rule refresh, nor wait 6 h for a sync."""
        from unittest import mock
        from app import scheduler
        added = {}
        class Fake:
            timezone = __import__("zoneinfo").ZoneInfo("Asia/Kolkata")
            running = False
            def add_job(self, fn, trigger, id=None, **kw): added[id] = (fn, type(trigger).__name__)
            def start(self): pass
        with mock.patch.object(scheduler, "BackgroundScheduler", return_value=Fake()):
            scheduler.start_scheduler()
        self.assertEqual(added["startup_sync"], (scheduler._sync_job, "DateTrigger"))
        self.assertEqual(added["startup_rules"], (scheduler._live_rules_job, "DateTrigger"))

    def test_scheduler_and_sync_button_use_the_pipeline_not_their_own_copies(self):   # mutation guards
        from app import scheduler
        sched = inspect.getsource(scheduler._sync_job)                      # the other jobs (daily snapshot) are separate
        route = (APP / "api" / "routes" / "sync.py").read_text()
        self.assertIn("pipeline.scheduled_run", sched)
        self.assertIn('id="live_rules"', (APP / "scheduler.py").read_text())
        for src in (sched, route):
            self.assertNotIn("ingest_alerts(", src)
            self.assertNotIn("snapshot_all(", src)
            self.assertNotIn("_ledger_refresh(", src)
        self.assertIn("pipeline.ingest_evidence", route)
        self.assertEqual(route.count("pipeline.rebuild(db)"), 2)          # full sync and reprocess


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class WithDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.models import database, schemas
        from app.models.database import Base
        from tests.dbhelp import reset_schema
        cls.engine = create_engine(URL)
        reset_schema(cls.engine)
        Base.metadata.create_all(cls.engine)
        with mock.patch.object(database, "engine", cls.engine):          # builds the views exactly as the app does
            from app.main import _migrate_db
            _migrate_db()
        cls.m, cls.db = schemas, sessionmaker(bind=cls.engine)()

    @classmethod
    def tearDownClass(cls):
        cls.db.rollback(); cls.db.close(); cls.engine.dispose()

    def setUp(self):
        self.addCleanup(_reset)
        m = self.m
        for model in (m.RuleObservation, m.AppSetting, m.LoanAnchor, m.Owner):
            self.db.query(model).delete()
        self.db.commit()
        _reset()

    # ── live rules ──
    def _fetch(self, value):
        return {"rbi.repo_rate": ((lambda: value) if not isinstance(value, Exception) else mock.Mock(side_effect=value), live.RBI_HOME)}

    def test_first_read_that_agrees_with_the_file_is_recorded_and_changes_nothing_else(self):
        out = live.refresh(self.db, self._fetch(D("5.50")), today=date(2026, 12, 1))
        self.assertEqual(out, {"rbi.repo_rate": "unchanged"})
        self.assertEqual(self.db.query(self.m.RuleObservation).count(), 1)
        self.assertEqual(rules.rule("rbi.repo_rate", date(2026, 12, 1)).verified_on, date(2026, 12, 1))

    def test_same_value_again_extends_one_row_not_a_new_one(self):
        live.refresh(self.db, self._fetch(D("5.50")), today=date(2026, 12, 1))
        live.refresh(self.db, self._fetch(D("5.50")), today=date(2026, 12, 2))
        (row,) = self.db.query(self.m.RuleObservation).all()
        self.assertEqual((row.first_seen, row.last_seen), (date(2026, 12, 1), date(2026, 12, 2)))

    def test_a_rate_change_is_picked_up_and_history_is_kept(self):
        live.refresh(self.db, self._fetch(D("5.50")), today=date(2026, 12, 1))
        out = live.refresh(self.db, self._fetch(D("5.75")), today=date(2026, 12, 5))
        self.assertEqual(out, {"rbi.repo_rate": "changed 5.5 → 5.75"})
        self.assertEqual(rules.value("rbi.repo_rate", date(2026, 12, 6)), D("5.75"))
        self.assertEqual(rules.value("rbi.repo_rate", date(2026, 12, 2)), D("5.50"))
        self.assertEqual(self.db.query(self.m.RuleObservation).count(), 2)

    def test_an_unreachable_or_unreadable_page_changes_nothing(self):
        self.assertEqual(live.refresh(self.db, self._fetch(OSError("offline")), today=date(2026, 12, 1)),
                         {"rbi.repo_rate": "unavailable"})
        self.assertEqual(live.refresh(self.db, self._fetch(None), today=date(2026, 12, 1)),
                         {"rbi.repo_rate": "unreadable"})
        self.assertEqual(self.db.query(self.m.RuleObservation).count(), 0)
        self.assertEqual(rules.value("rbi.repo_rate"), D("5.50"))

    def test_observations_survive_a_restart(self):
        live.refresh(self.db, self._fetch(D("5.75")), today=date(2026, 12, 5))
        _reset()                                                    # a new process starts with only the file
        self.assertEqual(rules.value("rbi.repo_rate", date(2026, 12, 6)), D("5.50"))
        self.assertEqual(live.load_observations(self.db), 1)
        self.assertEqual(rules.value("rbi.repo_rate", date(2026, 12, 6)), D("5.75"))

    def test_the_deadlines_card_notices_a_change_by_itself(self):
        from app.engine import deadlines
        self.db.add(self.m.Owner(id="user", name="u"))
        self.db.add(self.m.LoanAnchor(owner_id="user", lender="HDFC", loan_no="1", as_on=date(2026, 10, 1), prepaid=D(0),
                                      principal_after=D(10000000), emi=D(170000), rate=D("7.1"), natural_key="k"))
        self.db.commit()
        live.refresh(self.db, self._fetch(D("5.75")), today=date(2026, 12, 5))
        (item,) = [i for i in deadlines.build(self.db, today=date(2026, 12, 10))["items"] if i["kind"] == "loan_repo_change"]
        self.assertEqual((item["date"], item["title"]), ("2026-12-05", "Repo rate now 5.75%"))

    # ── re-confirming rules from their pages ──
    def _pages(self, miss=(), down=()):
        def fetch(url):
            if url in down:
                raise OSError("down")
            rid_for = [rules.rule(rid) for rid in live.probed_rule_ids() if (rules.rule(rid).probe_url or rules.rule(rid).source) == url]
            return " ".join(_sample(r.probe) for r in rid_for if url not in miss) or "nothing relevant"
        return fetch

    def test_confirmed_pages_refresh_verified_on_and_missing_or_down_pages_write_nothing(self):
        urls = {(rules.rule(r).probe_url or rules.rule(r).source) for r in live.probed_rule_ids()}
        gone, down = sorted(urls)[0], sorted(urls)[1]
        out = live.verify_sources(self.db, fetch=self._pages(miss={gone}, down={down}), today=date(2026, 12, 1))
        statuses = set(out.values())
        self.assertEqual(statuses, {"confirmed", "not confirmed", "unreachable"})
        confirmed = [r for r, v in out.items() if v == "confirmed"]
        self.assertTrue(confirmed)
        self.assertEqual(rules.rule(confirmed[0], date(2026, 12, 1)).verified_on, date(2026, 12, 1))     # nobody edited a file
        for rid in (r for r, v in out.items() if v != "confirmed"):
            self.assertEqual(rules.rule(rid, date(2026, 12, 1)).verified_on, date(2026, 10, 12))
        self.assertEqual(self.db.query(self.m.RuleObservation).count(), len(confirmed))

    def test_a_confirmation_never_changes_a_value(self):
        before = {rid: rules.value(rid, date(2026, 12, 1)) for rid in live.probed_rule_ids()}
        live.verify_sources(self.db, fetch=self._pages(), today=date(2026, 12, 1))
        self.assertEqual({rid: rules.value(rid, date(2026, 12, 1)) for rid in live.probed_rule_ids()}, before)

    # ── the check that notices a silent stop ──
    def test_automation_check_states(self):
        from app import consistency
        from app.etl import pipeline
        self.assertEqual(consistency.automation_is_running(self.db)["severity"], "warn")           # never ran
        pipeline.record_run(self.db, "scheduler", True)
        self.assertTrue(consistency.automation_is_running(self.db)["ok"])
        pipeline.record_run(self.db, "scheduler", False, "imap down")
        c = consistency.automation_is_running(self.db)
        self.assertFalse(c["ok"]); self.assertIn("imap down", c["detail"])
        pipeline.record_run(self.db, "scheduler", True)
        row = self.db.get(self.m.AppSetting, pipeline.RUN_KEY)
        import json
        stale = json.loads(row.value); stale["at"] = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()
        row.value = json.dumps(stale); self.db.commit()
        c = consistency.automation_is_running(self.db)
        self.assertFalse(c["ok"]); self.assertIn("40 h ago", c["detail"])

    def test_the_check_is_registered(self):
        from app import consistency
        self.assertIn(consistency.automation_is_running, consistency.CHECKS)


class MissedJobs(unittest.TestCase):
    """The laptop sleeps: jobs missed overnight must still run once on wake, and an overdue update must not wait for the next interval.
    Real case (Oct 2026): the 00:05 snapshot was 'missed by 23:07' and dropped, the 6-hourly sync 'missed by 0:38' (grace was 300 s)."""

    def test_every_job_coalesces_and_has_a_grace_that_survives_a_night(self):
        from app import scheduler
        sched = scheduler._build_scheduler()
        sched.start(paused=True)                                   # defaults are applied when the jobs reach the store
        self.addCleanup(sched.shutdown, wait=False)
        jobs = {j.id: j for j in sched.get_jobs()}
        for jid in ("email_sync", "networth_snapshot", "holdings_autotag", "live_rules", "goal_update"):
            self.assertTrue(jobs[jid].coalesce, jid)                                   # one catch-up run, not one per missed tick
            self.assertGreaterEqual(jobs[jid].misfire_grace_time, 12 * 3600, jid)
        self.assertGreaterEqual(jobs["goal_update"].misfire_grace_time, 6 * 86400)
        self.assertIn("update_watchdog", jobs)

    def test_overdue_rule(self):
        from app import scheduler
        now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        ran = lambda h: {"at": (now - timedelta(hours=h)).isoformat(), "ok": True}
        self.assertFalse(scheduler.update_is_overdue(ran(5), now, 6))                  # not due yet
        self.assertFalse(scheduler.update_is_overdue(ran(6.4), now, 6))                # inside the slack
        self.assertTrue(scheduler.update_is_overdue(ran(7), now, 6))                   # a run was missed
        self.assertTrue(scheduler.update_is_overdue(None, now, 6))                     # never ran
        self.assertTrue(scheduler.update_is_overdue({"at": "garbage"}, now, 6))
        failed = {"at": (now - timedelta(hours=2)).isoformat(), "ok": False}
        self.assertFalse(scheduler.update_is_overdue(failed, now, 6))                  # a failed attempt is retried next interval, not every tick

    def test_the_watchdog_runs_the_update_only_when_it_is_overdue_and_idle(self):
        from app import scheduler
        from app.etl import pipeline
        with mock.patch.object(scheduler, "_sync_job") as sync, mock.patch("app.models.database.SessionLocal"), \
                mock.patch.object(pipeline, "last_run", return_value=None):
            scheduler._watchdog_job()
            sync.assert_called_once()                                                  # never ran: overdue
            sync.reset_mock()
            self.assertTrue(pipeline.RUN_LOCK.acquire(blocking=False))
            try:
                scheduler._watchdog_job()
                sync.assert_not_called()                                               # an update is already running
            finally:
                pipeline.RUN_LOCK.release()


class StaleSnapshotIsAWarningNotAFailure(unittest.TestCase):
    """Deposits accrue interest by the day; the net-worth snapshot and stored goal SIPs are taken at the last update. One day apart
    they differ by that day's accrual: a missing update (reported by automatic_update_is_running), not a broken page."""

    yesterday = date.today() - timedelta(days=1)
    failing = {"name": "x", "ok": False, "severity": "fail", "detail": "differs", "values": {}}

    def test_small_accrual_difference_with_a_stale_snapshot_is_a_warning(self):
        from app import consistency
        out = consistency._if_stale(self.failing, self.yesterday)
        self.assertEqual(out["severity"], "warn"); self.assertIn("snapshot is from", out["detail"])

    def test_a_current_snapshot_keeps_the_failure(self):
        from app import consistency
        self.assertEqual(consistency._if_stale(self.failing, None)["severity"], "fail")

    def test_a_difference_too_large_to_be_interest_stays_a_failure(self):
        from app import consistency
        self.assertTrue(consistency.within_accrual(635309.44, 635200.98, self.yesterday))     # ₹108 on ₹635k: one day of interest
        self.assertFalse(consistency.within_accrual(635309.44, 535200.98, self.yesterday))    # ₹1 lakh is not interest
        for stored, live in ((13625.24, 13636.93), (147676.95, 147759.11)):                   # the real SIP differences of 19 Oct
            self.assertTrue(consistency.within_accrual(stored, live, self.yesterday, 0.005))
        self.assertFalse(consistency.within_accrual(13625.24, 15000.00, self.yesterday, 0.005))
        self.assertEqual(consistency._if_stale(self.failing, self.yesterday, small=False)["severity"], "fail")

    def test_the_invariant_lines_that_are_only_accrual(self):
        from app import consistency
        ok = "wife: fd is ₹635,309 on Investments but ₹635,201 in net worth"
        self.assertTrue(consistency._is_small_accrual(ok, self.yesterday))
        self.assertTrue(consistency._is_small_accrual("wife: deposits are ₹635,309 on Manual Assets but ₹635,201 in net worth", self.yesterday))
        self.assertFalse(consistency._is_small_accrual("wife: fd is ₹635,309 on Investments but ₹135,201 in net worth", self.yesterday))
        self.assertFalse(consistency._is_small_accrual("wife: equity+bonds is ₹635,309 on Investments but ₹635,201 in net worth", self.yesterday))


if __name__ == "__main__":
    unittest.main()
