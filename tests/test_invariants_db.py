"""
Cross-page invariants (app/invariants.py) against a scratch household built to be awkward:
a PPF with no dated contributions, a PPF built from transfers, deposits that can and cannot be measured,
a fund bought and fully sold, units with no history, a bank-tagged SIP, an SGB, a loan with lender letters.

Also proves the guard has teeth: each "mutation" re-creates a bug this project has actually shipped
(an asset dropped from a page, a page counted differently) and the matching invariant must catch it.
Needs a scratch Postgres (TEST_DATABASE_URL); never run it against real data.
"""
import os
import unittest
import uuid
from datetime import date, timedelta
from decimal import Decimal as D
from unittest import mock

URL = os.getenv("TEST_DATABASE_URL")


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class Invariants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker
        from app.models import schemas
        from app.models import database
        from app.models.database import Base
        cls.engine = create_engine(URL)
        from tests.dbhelp import reset_schema
        reset_schema(cls.engine)
        Base.metadata.create_all(cls.engine)
        with mock.patch.object(database, "engine", cls.engine):          # builds the views exactly as the app does
            from app.main import _migrate_db
            _migrate_db()
        cls.S, cls.m = sessionmaker(bind=cls.engine), schemas
        cls.db = cls.S()
        cls._world()

    @classmethod
    def tearDownClass(cls):
        cls.db.rollback()          # an open transaction would block the next module's DROP SCHEMA
        cls.db.close()
        cls.engine.dispose()

    @classmethod
    def _world(cls):
        m, db, today = cls.m, cls.db, date.today()
        for oid, name in (("user", "Swetank Gupta"), ("wife", "Akanksha Arya")):
            db.add(m.Owner(id=oid, name=name))
        db.flush()

        def acct(owner, inst, typ, ref="1"):
            a = m.Account(owner_id=owner, institution=inst, account_type=typ, account_ref=ref)
            db.add(a); db.flush(); return a

        hdfc, zer = acct("user", "HDFC", "bank", "1632"), acct("user", "Zerodha", "stocks", "z")
        wzer = acct("wife", "Zerodha", "stocks", "w")

        def txn(a, d, amt, kind, isin=None, desc="x"):
            db.add(m.Transaction(account_id=a.id, date=d, amount=D(amt), transaction_type=kind, isin=isin,
                                 description=desc, raw_description=desc, fingerprint=uuid.uuid4().hex))

        def hold(a, isin, name, typ, value, units, quality, invested=None, first=None):
            db.add(m.Holding(account_id=a.id, snapshot_date=today, isin=isin, instrument_name=name, holding_type=typ,
                             current_value=D(value), units=D(units), cost_basis_quality=quality,
                             invested_value=D(invested) if invested else None, first_buy_date=first,
                             source_file="ledger:rollforward"))

        # equity: bought in two lots, still held
        hold(zer, "INE001A01011", "ACME LTD", "equity", 120000, 100, "exact", 100000, today - timedelta(days=500))
        txn(zer, today - timedelta(days=500), 60000, "debit", "INE001A01011")
        txn(zer, today - timedelta(days=300), 40000, "debit", "INE001A01011")
        # a fund bought via a BANK-account SIP mandate (decision 23: flows count wherever they sit)
        hold(zer, "INF001A01011", "SIP FUND", "mutual_fund", 55000, 500, "exact", 50000, today - timedelta(days=700))
        for k in range(5):
            txn(hdfc, today - timedelta(days=700 - 90 * k), 10000, "debit", "INF001A01011", "ACH SIP")
        # a fund bought and fully sold (realised history only)
        txn(zer, today - timedelta(days=900), 20000, "debit", "INF002A01011")
        txn(zer, today - timedelta(days=400), 24000, "credit", "INF002A01011")
        for side, qty, d in (("B", 100, today - timedelta(days=900)), ("S", 100, today - timedelta(days=400))):
            db.add(m.InstrumentTrade(owner_id="user", account_id=zer.id, isin="INF002A01011", instrument_name="SOLD FUND",
                                     holding_type="mutual_fund", trade_date=d, side=side, quantity=D(qty), source="coin",
                                     natural_key=uuid.uuid4().hex))
        # the wife's units with no purchase history at all
        hold(wzer, "INF003A01011", "NO HISTORY FUND", "mutual_fund", 80000, 800, "partial")
        # an SGB: no trade, cost from the issue price
        hold(wzer, "IN0020230069", "SGB 2031", "bonds", 150000, 25, "exact", 146825, date(2023, 9, 14))

        def asset(owner, typ, name, value, **kw):
            a = m.ManualAsset(owner_id=owner, asset_type=typ, name=name, institution=kw.pop("inst", "SBI"),
                              current_value=D(value), **kw)
            db.add(a); db.flush(); return a

        p1 = asset("user", "ppf", "Union Bank PPF", 540000, invested_amount=D(450000), start_date=date(2022, 4, 27),
                   interest_rate=D("7.1"), inst="Union Bank")
        for d, amt in ((date(2022, 4, 27), 150000), (date(2024, 5, 31), 150000), (date(2025, 8, 3), 150000)):
            db.add(m.ManualAssetContribution(asset_id=p1.id, date=d, amount=D(amt)))
        asset("wife", "ppf", "SBI PPF", 1900000, invested_amount=D(1700000), interest_rate=D("7.1"))   # no dates at all
        asset("user", "fd", "SBI FD ...1111", 100000, invested_amount=D(100000), start_date=today - timedelta(days=400),
              maturity_date=today + timedelta(days=300), interest_rate=D("6.25"),
              notes="Maturity value: ₹100,000.00", accrued_interest=D("450"), source_statement_date=today - timedelta(days=10))
        asset("wife", "fd", "SBI FD ...2222", 50000, invested_amount=D(80000), start_date=today - timedelta(days=500),
              maturity_date=today + timedelta(days=200), interest_rate=D("6.25"))
        asset("wife", "fd", "SBI FD ...3333", 30000, maturity_date=today - timedelta(days=10))               # matured
        # a cumulative deposit: the statement amount plus the interest in its maturity value, accrued to today
        asset("wife", "fd", "SBI FD ...4444", 100000, invested_amount=D(100000), start_date=today - timedelta(days=200),
              maturity_date=today + timedelta(days=165), interest_rate=D("6.25"),
              notes="Maturity value: ₹106,400.00", source_statement_date=today - timedelta(days=30),
              accrued_interest=D("3000"))
        asset("user", "real_estate", "Flat", 4_000_000_0, invested_amount=D(3_000_000_0))
        asset("user", "liability", "Home Loan", -13000000, inst="HDFC", invested_amount=D(20000000),
              start_date=date(2024, 1, 15), source_statement_date=date(2026, 4, 1), interest_rate=D("7.1"))
        # savings-account interest credits that measure FD ...1111 (monthly, nearly complete)
        for k in range(1, 13):
            txn(hdfc, today - timedelta(days=30 * k), 520, "credit", None, "INT TRF FRM 40000001111 TO 30778223316")
        db.add(m.LoanAnchor(owner_id="user", lender="HDFC", loan_no="1", as_on=date(2026, 5, 3), prepaid=D(0),
                            principal_after=D(12803080), emi=D(170000), rate=D("7.1"), natural_key="t1"))
        db.commit()
        from app.engine.loan import apply_loan_anchors             # the same order as the maintenance pipeline
        from app.engine.networth import snapshot_all
        apply_loan_anchors(db)
        snapshot_all(db)

    def run_all(self):
        from app.invariants import INVARIANTS
        return {name: fn(self.db) for name, fn in INVARIANTS}

    # ── the household satisfies every invariant ────────────────────────────────
    def test_every_invariant_holds_for_the_awkward_household(self):
        broken = {k: v for k, v in self.run_all().items() if v}
        self.assertEqual(broken, {})

    def test_the_awkward_cases_are_really_in_play(self):
        from app.engine.portfolio_xirr import compute_owner_returns
        fam = compute_owner_returns(self.db, "family")
        names = {m["name"] for m in fam["manual_assets"]}
        self.assertIn("SBI PPF", names)                                  # PPF with no dated contributions is listed
        self.assertNotIn("SBI FD ...3333", names)                        # matured deposits are not
        by = {c["class"]: c for c in fam["by_class"]}
        self.assertAlmostEqual(by["ppf"]["current_all"], 2_440_000, delta=1)
        self.assertLess(by["mutual_fund"]["coverage_pct"], 100)          # the no-history fund is named, not hidden
        self.assertIsNotNone(by["mutual_fund"]["xirr_pct"])              # ...and does not blank the class
        self.assertEqual(sum(1 for i in fam["instruments"] if i["cost_quality"] == "partial"), 1)

    def test_a_bank_sip_debit_counts_as_a_purchase(self):
        from app.engine.portfolio_xirr import compute_owner_returns
        inst = {i["isin"]: i for i in compute_owner_returns(self.db, "user")["instruments"]}
        self.assertEqual(inst["INF001A01011"]["invested"], 50000.0)
        self.assertIsNotNone(inst["INF001A01011"]["xirr_pct"])

    def test_a_cumulative_deposit_is_worth_its_accrued_value_on_every_page(self):
        from app.api.routes.manual import _value_today
        from app.consistency import _latest_nw
        from app.engine.portfolio_xirr import compute_owner_returns
        from app.models.schemas import ManualAsset
        a = self.db.query(ManualAsset).filter_by(name="SBI FD ...4444").one()
        accrued = _value_today(a)
        self.assertTrue(103000 < accrued < 106400)                              # principal + accrued interest, grown on, not yet at maturity
        row = next(m for m in compute_owner_returns(self.db, "wife")["manual_assets"] if m["name"] == "SBI FD ...4444")
        self.assertAlmostEqual(row["current"], accrued, delta=0.01)             # Investments
        self.assertAlmostEqual(row["rate"], 6.2, delta=0.4)                     # the rate the statement's own numbers imply
        self.assertTrue(row["contract"])
        wife_fd = float(_latest_nw(self.db)["wife"].fd_value)
        self.assertTrue(wife_fd >= accrued)                                     # net worth includes the accrued interest

    def test_a_deposit_with_monthly_interest_on_file_is_measured_not_stated(self):
        from app.engine.portfolio_xirr import compute_owner_returns
        row = next(m for m in compute_owner_returns(self.db, "user")["manual_assets"] if m["name"] == "SBI FD ...1111")
        self.assertIsNotNone(row["xirr_pct"])
        self.assertAlmostEqual(row["xirr_pct"], 6.4, delta=1.0)

    # ── mutations: re-create bugs this project shipped; the invariants must notice ─────────────────
    def test_catches_an_asset_dropped_from_the_page(self):
        from app.engine import portfolio_xirr as px
        real = px._manual_rows
        with mock.patch.object(px, "_manual_rows", lambda ps, t: [r for r in real(ps, t) if r["type"] != "ppf"]):
            from app.invariants import inv01_every_manual_investment_is_listed
            self.assertTrue(any("PPF" in v for v in inv01_every_manual_investment_is_listed(self.db)))

    def test_catches_a_class_missing_from_the_total(self):
        from app.engine import portfolio_xirr as px
        real = px.compute_owner_returns

        def broken(db, owner, today=None):
            r = real(db, owner, today)
            r["total"]["value_all"] = r["total"]["value_all"] - 100000          # a page that forgot something
            return r
        with mock.patch("app.invariants._returns", lambda db, o: broken(db, o)):
            from app.invariants import inv02_classes_add_up_to_total_value
            self.assertTrue(inv02_classes_add_up_to_total_value(self.db))

    def test_catches_returns_disagreeing_with_net_worth(self):
        from app.models.schemas import NetWorthSnapshot
        row = self.db.query(NetWorthSnapshot).filter_by(owner_id="wife").order_by(NetWorthSnapshot.snapshot_date.desc()).first()
        keep = row.ppf_value
        row.ppf_value = keep + D(50000)
        self.db.flush()
        try:
            from app.invariants import inv03_investments_match_net_worth
            self.assertTrue(any("ppf" in v for v in inv03_investments_match_net_worth(self.db)))
        finally:
            row.ppf_value = keep
            self.db.flush()

    def test_catches_a_holding_missing_from_investments(self):
        from app.engine import portfolio_xirr as px
        real = px._instruments
        with mock.patch.object(px, "_instruments", lambda ps, t: real(ps, t)[1:]):
            from app.invariants import inv06_every_holding_is_listed
            self.assertTrue(inv06_every_holding_is_listed(self.db))

    def test_a_figure_you_typed_later_is_never_overwritten_by_an_older_letter(self):
        from app.engine.loan import apply_loan_anchors
        from app.models.schemas import ManualAsset
        a = self.db.query(ManualAsset).filter_by(asset_type="liability").one()
        before = (a.current_value, a.source_statement_date)
        a.current_value, a.source_statement_date = D(-11111111), date.today()
        self.db.flush()
        try:
            apply_loan_anchors(self.db)
            self.db.refresh(a)
            self.assertEqual(a.current_value, D(-11111111))
        finally:
            a.current_value, a.source_statement_date = before
            self.db.flush()

    def test_catches_a_loan_that_ignores_the_lender(self):
        from app.models.schemas import ManualAsset
        a = self.db.query(ManualAsset).filter_by(asset_type="liability").one()
        before = (a.current_value, a.source_statement_date)
        a.current_value, a.source_statement_date = D(-99999999), date(2026, 1, 1)      # stale and wrong
        self.db.flush()
        try:
            from app.invariants import inv07_loan_follows_the_lender
            self.assertTrue(inv07_loan_follows_the_lender(self.db))
        finally:
            a.current_value, a.source_statement_date = before
            self.db.flush()


if __name__ == "__main__":
    unittest.main()
