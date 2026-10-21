"""Emergency fund = bank + every live FD/RD; every other goal's corpus = everything tagged to it, manual assets included. Scratch database (TEST_DATABASE_URL)."""
import os
import unittest
from datetime import date
from decimal import Decimal as D
from unittest import mock

URL = os.getenv("TEST_DATABASE_URL")


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class EmergencyCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.models import database, schemas  # noqa: F401
        from app.models.database import Base
        from tests.dbhelp import reset_schema
        cls.engine = create_engine(URL)
        reset_schema(cls.engine)
        Base.metadata.create_all(cls.engine)
        with mock.patch.object(database, "engine", cls.engine):
            from app.main import _migrate_db
            _migrate_db()
        cls.db = sessionmaker(bind=cls.engine)()
        from app.models.schemas import Owner
        cls.db.add_all([Owner(id="user", name="U"), Owner(id="wife", name="W"), Owner(id="family", name="F")])
        cls.db.commit()

    def test_deposits_are_the_emergency_fund_and_a_goal_counts_its_tagged_manual_assets(self):
        from app.engine.goals import refresh_corpus_from_networth
        from app.models.schemas import Goal, ManualAsset, NetWorthSnapshot
        from app.planning.matching import deposits_to_emergency
        db = self.db
        db.add_all([ManualAsset(owner_id="user", asset_type="fd", name="FD", current_value=D(100_000), goal_tag="health", goal_tag_source="auto"),
                    ManualAsset(owner_id="user", asset_type="rd", name="RD", current_value=D(50_000), goal_tag="child_marriage"),
                    ManualAsset(owner_id="user", asset_type="sgb", name="SGB", current_value=D(70_000), goal_tag="health", goal_tag_source="auto")])
        db.add(NetWorthSnapshot(snapshot_date=date.today(), owner_id="family", bank_balance=D(200_000), fd_value=D(150_000)))
        em = Goal(owner_id="family", name="Emergency", goal_type="emergency", target_amount=D(1))
        hl = Goal(owner_id="family", name="Health", goal_type="health", target_amount=D(1))
        db.add_all([em, hl]); db.commit()
        self.assertEqual(deposits_to_emergency(db), 2)                       # the FD and the RD; the SGB is matched to a goal, not here
        db.commit()
        refresh_corpus_from_networth(db, "family")
        db.refresh(em); db.refresh(hl)
        self.assertEqual(em.current_corpus, D(350_000))                       # bank 2,00,000 + FDs/RDs 1,50,000
        self.assertEqual(hl.current_corpus, D(70_000))                        # the SGB counts; the FD moved to the emergency fund


if __name__ == "__main__":
    unittest.main()
