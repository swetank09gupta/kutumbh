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


def _reset():
    rules.set_observations([])


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
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].verified_on, date(2027, 1, 5))
        self.assertEqual(entries[0].effective_from, YAML_REPO.effective_from)      # the file's date is kept

    def test_a_changed_value_starts_a_new_entry_and_keeps_the_old_one(self):
        rules.set_observations([("rbi.repo_rate", "5.75", date(2026, 12, 5), date(2026, 12, 20), live.RBI_HOME)])
        self.assertEqual(rules.value("rbi.repo_rate", date(2026, 12, 4)), D("5.50"))      # history stays right
        self.assertEqual(rules.value("rbi.repo_rate", date(2026, 12, 5)), D("5.75"))
        self.assertEqual(rules.value("rbi.repo_rate", date(2026, 12, 25)), D("5.75"))
        self.assertIn("automatically", rules.rule("rbi.repo_rate", date(2026, 12, 25)).note)

    def test_a_later_change_back_is_a_third_entry(self):
        rules.set_observations([("rbi.repo_rate", "5.75", date(2026, 12, 5), date(2027, 2, 1), live.RBI_HOME),
                                ("rbi.repo_rate", "5.50", date(2027, 2, 5), date(2027, 2, 6), live.RBI_HOME)])
        self.assertEqual([r.value for r in rules.load()["rbi.repo_rate"]], [D("5.50"), D("5.75"), D("5.50")])
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


class OneSharedPipeline(unittest.TestCase):
    def test_rebuild_runs_in_dependency_order(self):
        from app.etl import pipeline
        calls = []
        with mock.patch("app.etl.maintenance.run_all", lambda db: calls.append("maintenance")), \
             mock.patch("app.ledger.refresh", lambda db: calls.append("ledger")), \
             mock.patch("app.engine.networth.snapshot_all", lambda db: calls.append("snapshot") or []), \
             mock.patch("app.engine.goals.refresh_goals", lambda db: calls.append("goals")), \
             mock.patch("app.consistency.run", lambda db: calls.append("consistency") or {}):
            pipeline.rebuild(object())
        self.assertEqual(calls, ["maintenance", "ledger", "snapshot", "goals", "consistency"])

    def test_one_failing_evidence_source_does_not_stop_the_others(self):
        from app.etl import pipeline
        db = mock.Mock()
        with mock.patch("app.ingestion.alert_fetcher.ingest_alerts", side_effect=RuntimeError("imap down")), \
             mock.patch("app.ingestion.loan_fetcher.ingest_loan_letters", return_value={"new": 1}) as loans, \
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


if __name__ == "__main__":
    unittest.main()
