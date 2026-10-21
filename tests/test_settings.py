"""Inflation fallback is derived from CPI data. Needs a scratch Postgres (TEST_DATABASE_URL)."""
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
        from tests.dbhelp import reset_schema
        reset_schema(cls.engine)
        Base.metadata.create_all(cls.engine)
        cls.S = sessionmaker(bind=cls.engine)

    STATS = {"latest": 2.4, "latest_year": "2025", "avg_10y": 4.74}

    def test_inflation_follows_the_ten_year_average(self):
        from unittest import mock
        from app.engine.settings_store import get_inflation
        with mock.patch("app.engine.inflation.get_india_cpi_stats", return_value=self.STATS):
            self.assertEqual(get_inflation(), 4.7)                   # the 10-year average, not last year's 2.4

    def test_goal_target_grows_at_the_automatic_rate_not_the_goals_own(self):
        from datetime import date
        from types import SimpleNamespace
        from unittest import mock
        from app.engine.goals import project_goal
        db = self.S()
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
