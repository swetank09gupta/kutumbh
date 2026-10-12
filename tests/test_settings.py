"""Inflation is one app-wide setting. Needs a scratch Postgres (TEST_DATABASE_URL)."""
import os
import unittest

URL = os.getenv("TEST_DATABASE_URL")


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class Inflation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker
        from app.models import schemas  # noqa: F401
        from app.models.database import Base
        cls.engine = create_engine(URL)
        with cls.engine.begin() as c:
            c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        Base.metadata.create_all(cls.engine)
        cls.S = sessionmaker(bind=cls.engine)

    STATS = {"latest": 2.4, "latest_year": "2025", "avg_10y": 4.74}

    def test_inflation_follows_the_ten_year_average_until_overridden(self):
        from unittest import mock
        from app.engine.settings_store import clear_inflation_override, get_inflation, set_inflation
        db = self.S()
        clear_inflation_override(db)
        with mock.patch("app.engine.inflation.get_india_cpi_stats", return_value=self.STATS):
            self.assertEqual(get_inflation(db), 4.7)                 # automatic, not last year's 2.4
            set_inflation(db, 5.5)
            self.assertEqual(get_inflation(self.S()), 5.5)           # another session sees the override
            clear_inflation_override(db)
            self.assertEqual(get_inflation(db), 4.7)                 # back to automatic
        db.close()

    def test_nonsense_override_is_refused(self):
        from app.engine.settings_store import set_inflation
        for bad in (-1, 25):
            with self.assertRaises(ValueError):
                set_inflation(self.S(), bad)

    def test_goal_target_grows_at_the_automatic_rate_not_the_goals_own(self):
        from datetime import date
        from types import SimpleNamespace
        from unittest import mock
        from app.engine.goals import project_goal
        from app.engine.settings_store import clear_inflation_override
        db = self.S()
        clear_inflation_override(db)
        g = SimpleNamespace(id=1, owner_id="user", name="g", goal_type="retirement", target_amount=1_000_000,
                            target_date=date(date.today().year + 10, 1, 1), current_corpus=0,
                            expected_return_rate=10.0, inflation_rate=2.0, use_manual_return=True,
                            sip_stepup_pct=0, glide_enabled=False, glide_start_years_before=0,
                            glide_end_return_pct=8.0, monthly_sip_needed=None, notes=None)
        with mock.patch("app.engine.inflation.get_india_cpi_stats", return_value=self.STATS):
            p = project_goal(g, 0, db=db)
        ratio = p.inflation_adj_target / 1_000_000
        self.assertTrue(1.047 ** 8.5 < ratio < 1.047 ** 10, ratio)   # ~9.2 years at 4.7%; at the goal's own 2% it would be ~1.2


if __name__ == "__main__":
    unittest.main()
