"""
IPO allotments and bonus issues from registrar mails (synthetic numbers shaped like the real LIC and Nykaa mails), matched to the
depository credit of the same quantity, and what the ledger then knows about cost.
"""
import os
import unittest
import uuid
from datetime import date
from decimal import Decimal as D

from app.ingestion.parsers.registrar_notice import BonusAllotment, IpoAllotment, parse_bonus, parse_ipo_allotment
from app.ledger.rules import Trade, compute_isin_state, effective_trades

URL = os.getenv("TEST_DATABASE_URL")
DEMAT = "1208160000000001"
IPO = (f"<p>Please find the allotment cum unblocking details as given below: Allotment advice cum Unblocking Intimation Bid cum Application "
       f"Form No. 5489813 DP ID Client ID {DEMAT} Issue Price per Security (Rs.) 904 Sole/ First Applicant Name TEST USER "
       "Investment Particulars Allotment Particulars Securities Applied For 30 Securities Allotted 20 Shares being credited on 12/05/2022 "
       "Amount Invested 27120</p>")
BONUS = ("Allotment of Bonus Equity Shares ... bonus equity shares, in the ratio of 5 (Five) fully paid equity shares of Re. 1/- each held ... "
         f"Your Bonus Equity Shares have been credited to below mentioned demat account. of Equity Shares held as on the Record Date i.e. 03.11.2022 "
         f"of Bonus Shares allotted and credited in demat account {DEMAT} 4 20 All future communications ...")


class Parsers(unittest.TestCase):
    def test_an_ipo_allotment(self):
        n = parse_ipo_allotment(IPO)
        self.assertEqual((n.applied, n.allotted, n.price, n.credited_on, n.demat_id), (30, 20, D(904), date(2022, 5, 12), DEMAT))

    def test_an_unsuccessful_application_allots_nothing(self):
        self.assertIsNone(parse_ipo_allotment(IPO.replace("Securities Allotted 20", "Securities Allotted 0")))

    def test_a_bonus_issue(self):
        n = parse_bonus(BONUS)
        self.assertEqual((n.held, n.bonus, n.demat_id), (4, 20, DEMAT))

    def test_other_mail_is_neither(self):
        for text in ("Your statement is ready", "Dividend intimation of Rs 10 per share"):
            self.assertIsNone(parse_ipo_allotment(text)); self.assertIsNone(parse_bonus(text))


def T(i, day, side, qty, price, source="contract_note"):
    return Trade(id=str(i), isin="INE0X", trade_date=day, side=side, qty=D(qty), price=None if price is None else D(price), source=source)


class LedgerKnowsTheCost(unittest.TestCase):
    def test_an_ipo_allotment_is_a_known_cost_lot(self):
        s = compute_isin_state("INE0X", [T(1, date(2022, 5, 12), "B", 20, 904, "ipo_allotment")], [], [(date(2022, 6, 30), D(20))])
        self.assertEqual((s.position.quality, s.position.invested, s.position.first_buy_date), ("exact", D(18080), date(2022, 5, 12)))

    def test_a_bonus_share_costs_nothing_and_that_is_known(self):
        s = compute_isin_state("INE0X", [T(1, date(2022, 6, 1), "B", 4, 1125), T(2, date(2022, 11, 15), "B", 20, 0, "bonus")], [],
                               [(date(2022, 12, 31), D(24))])
        self.assertEqual((s.position.quality, s.position.invested), ("exact", D(4500)))             # the original cost is untouched

    def test_a_zero_price_that_is_not_a_bonus_is_still_an_unknown_cost(self):
        s = compute_isin_state("INE0X", [T(1, date(2022, 11, 15), "B", 20, 0, "contract_note")], [], [(date(2022, 12, 31), D(20))])
        self.assertNotEqual(s.position.quality, "exact")

    def test_an_arrival_is_not_merged_with_a_same_day_market_trade(self):
        trades = [T(1, date(2022, 11, 15), "B", 5, 150), T(2, date(2022, 11, 15), "B", 20, 0, "bonus")]
        eff, superseded, _ = effective_trades(trades)
        self.assertEqual((len(eff), superseded), (2, []))


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class Matching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.models import schemas
        from app.models.database import Base
        from tests.dbhelp import reset_schema
        cls.engine = create_engine(URL)
        reset_schema(cls.engine)
        Base.metadata.create_all(cls.engine)
        cls.S, cls.m = sessionmaker(bind=cls.engine), schemas

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        from sqlalchemy import text
        with self.engine.begin() as c:
            c.execute(text("TRUNCATE instrument_trades, demat_movements, accounts, owners CASCADE"))
        self.db, m = self.S(), self.m
        self.db.add(m.Owner(id="user", name="u")); self.db.flush()
        self.acct = m.Account(owner_id="user", institution="Zerodha", account_type="stocks", account_ref="z")
        self.db.add(self.acct); self.db.commit()
        self.addCleanup(self.db.close)

    def credit(self, isin, day, qty, symbol="STOCK", desc=f"CA {DEMAT}"):
        self.db.add(self.m.DematMovement(owner_id="user", account_id=self.acct.id, isin=isin, symbol=symbol, move_date=day, qty_in=D(qty), qty_out=D(0), description=desc,
                                         natural_key=uuid.uuid4().hex))
        self.db.commit()

    def store(self, *found):
        from app.ingestion.registrar_fetcher import store_notices
        return store_notices(self.db, [("user", n, d, "mid") for n, d in found])

    def trades(self):
        return self.db.query(self.m.InstrumentTrade).order_by(self.m.InstrumentTrade.trade_date).all()

    def test_an_allotment_becomes_a_trade_at_the_issue_price_on_the_credit_day(self):
        self.credit("INE0J1Y01017", date(2022, 5, 12), 20, "LIFE INSURANCE-EQ")
        self.assertEqual(self.store((parse_ipo_allotment(IPO), date(2022, 5, 13)))["ipo_allotment"], 1)
        (t,) = self.trades()
        self.assertEqual((t.isin, t.source, t.side, t.quantity, t.price, t.amount, t.trade_date, t.settle_date),
                         ("INE0J1Y01017", "ipo_allotment", "B", D(20), D(904), D(18080), date(2022, 5, 12), date(2022, 5, 12)))

    def test_a_bonus_becomes_a_nil_cost_trade_on_the_credit_day(self):
        self.credit("INE388Y01029", date(2022, 11, 15), 20, "FSN E-COMMERCE-")
        self.store((parse_bonus(BONUS), date(2022, 11, 16)))
        (t,) = self.trades()
        self.assertEqual((t.source, t.quantity, t.price, t.amount, t.trade_date), ("bonus", D(20), D(0), D(0), date(2022, 11, 15)))

    def test_a_bonus_that_first_lands_under_a_temporary_isin_is_booked_under_the_real_one(self):
        self.credit("IN8388Y01010", date(2022, 11, 14), 20, "FSN E-COMMERCE-"); self.credit("INE388Y01029", date(2022, 11, 16), 20, "FSN E-COMMERCE-")
        self.assertEqual(self.store((parse_bonus(BONUS), date(2022, 11, 16)))["bonus"], 1)
        (t,) = self.trades()
        self.assertEqual((t.isin, t.trade_date), ("INE388Y01029", date(2022, 11, 16)))

    def test_storing_twice_adds_nothing(self):
        self.credit("INE0J1Y01017", date(2022, 5, 12), 20)
        n = parse_ipo_allotment(IPO)
        self.store((n, date(2022, 5, 13))); self.store((n, date(2022, 5, 13)))
        self.assertEqual(len(self.trades()), 1)

    def test_two_stocks_credited_the_same_quantity_that_week_are_not_guessed(self):
        self.credit("INE0A0000001", date(2022, 5, 12), 20); self.credit("INE0B0000002", date(2022, 5, 11), 20)
        self.assertEqual(self.store((parse_ipo_allotment(IPO), date(2022, 5, 13)))["unmatched"], 1)
        self.assertEqual(self.trades(), [])

    def test_no_depository_credit_of_that_quantity_means_no_trade(self):
        self.credit("INE0J1Y01017", date(2022, 5, 12), 15)
        self.assertEqual(self.store((parse_ipo_allotment(IPO), date(2022, 5, 13)))["unmatched"], 1)

    def test_a_credit_in_another_demat_is_not_this_notice(self):
        self.credit("INE0J1Y01017", date(2022, 5, 12), 20, desc="CA 1208169999999999")
        self.assertEqual(self.store((parse_ipo_allotment(IPO), date(2022, 5, 13)))["unmatched"], 1)

    def test_the_fetch_is_part_of_the_evidence_step(self):
        import inspect
        from app.etl import pipeline
        self.assertIn("ingest_registrar_notices", inspect.getsource(pipeline.ingest_evidence))


if __name__ == "__main__":
    unittest.main()
