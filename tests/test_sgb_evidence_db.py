"""
SGB evidence on a scratch database, built from the real patterns: coupons arrive as truncated "CENTRAL DEPOSITO" NEFT credits
or RBI "Sovereign Gold B" credits; one subscription debit says "SGBS 25 GMS" and equals units x the ONLINE price; one
coupon stream belongs to a bond that no statement lists. Synthetic amounts derived from the public tranche prices.
Needs a scratch Postgres (TEST_DATABASE_URL).
"""
import os
import unittest
import uuid
from datetime import date, timedelta
from decimal import Decimal as D
from unittest import mock

from app.ledger import sgb, sgb_evidence as ev

URL = os.getenv("TEST_DATABASE_URL")
TODAY = date(2026, 10, 12)
VII, III_22, I_23 = "IN0020210178", "IN0020220110", "IN0020230069"
I_22 = "IN0020220045"


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class SgbWorld(unittest.TestCase):
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
        with mock.patch.object(database, "engine", cls.engine):
            from app.main import _migrate_db
            _migrate_db()
        cls.S, cls.m = sessionmaker(bind=cls.engine), schemas

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        from sqlalchemy import text
        with self.engine.begin() as c:
            c.execute(text("TRUNCATE transactions, statement_balances, manual_assets, accounts, owners CASCADE"))
        self.db, m = self.S(), self.m
        for o in ("user", "wife"):
            self.db.add(m.Owner(id=o, name=o))
        self.db.flush()
        self.bank = {o: m.Account(owner_id=o, institution="SBI", account_type="bank", account_ref=o) for o in ("user", "wife")}
        self.db.add_all(self.bank.values()); self.db.commit()
        self.addCleanup(self.db.close)

    # ── builders ──
    def held(self, owner, isin, units=25):
        self.db.add(self.m.StatementBalance(owner_id=owner, family="cdsl", as_on=date(2026, 9, 30), isin=isin, qty=D(units)))
        self.db.commit()

    def txn(self, owner, d, amount, kind, desc, **kw):
        t = self.m.Transaction(account_id=self.bank[owner].id, date=d, amount=D(amount), transaction_type=kind,
                               description=desc, raw_description=desc, fingerprint=uuid.uuid4().hex,
                               category=kw.get("category", "transfer"), category_source=kw.get("source", "rule"))
        self.db.add(t); self.db.commit(); return t

    def coupons(self, owner, isin, units=25, skip=(), desc="NEFT*ICIC0099999*CMS1*CENTRAL DEPOSITO", upto=TODAY, shift=1):
        t = sgb.tranche(isin)
        for d in ev.coupon_dates(t, upto):
            if d not in skip:
                self.txn(owner, d + timedelta(days=shift), t.coupon(D(units)), "credit", desc)

    def coverage(self, owner):                                    # the statements run to August 2026
        self.txn(owner, date(2026, 8, 20), 500, "debit", "UPI/GROCERY")

    # ── matching ──
    def test_a_complete_stream_matches_every_coupon_and_none_is_missing(self):
        self.held("user", III_22); self.coupons("user", III_22); self.coverage("user")
        (s,) = ev.scan(self.db, ["user"], TODAY)["streams"]
        self.assertEqual((s["series"], s["units"], s["held"], len(s["coupons"])), ("2022-23 Series III", 25, True, 7))
        self.assertEqual(s["missing_after_last"], [])
        self.assertAlmostEqual(s["received_total"], 7 * 1690.31, delta=0.1)
        self.assertEqual(s["next_coupon"], "2026-12-27")

    def test_a_stopped_stream_names_the_missing_coupons_only_where_the_statements_reach(self):
        self.held("user", III_22)
        self.coupons("user", III_22, upto=date(2025, 6, 30))                                  # coupons stop after Jun 2025
        self.coverage("user")
        (s,) = ev.scan(self.db, ["user"], TODAY)["streams"]
        self.assertEqual(s["missing_after_last"], ["2025-12-27", "2026-06-27"])               # Dec 2026 is not yet due

    def test_no_statement_beyond_a_coupon_means_it_cannot_be_called_missing(self):
        self.held("user", III_22)
        self.coupons("user", III_22, upto=date(2025, 6, 30))                                  # and the data ends before Dec 2025
        self.txn("user", date(2025, 7, 20), 500, "debit", "UPI/GROCERY")
        (s,) = ev.scan(self.db, ["user"], TODAY)["streams"]
        self.assertEqual(s["missing_after_last"], [])

    def test_a_coupon_stream_with_no_bond_in_any_statement_is_a_finding(self):
        self.coupons("wife", I_22, desc="NEFT*RBIS0MBPA04*U1*Sovereign Gold B", shift=2); self.coverage("wife")
        res = ev.scan(self.db, ["wife"], TODAY)
        (f,) = res["found_not_held"]
        self.assertEqual((f["series"], f["units"], f["held"], f["coupon_each"]), ("2022-23 Series I", 25, False, 1590.94))

    def test_a_bond_added_by_hand_resolves_the_finding(self):
        self.coupons("wife", I_22, desc="NEFT*RBIS0MBPA04*U1*Sovereign Gold B", shift=2); self.coverage("wife")
        self.db.add(self.m.ManualAsset(owner_id="wife", asset_type="sgb", name="SGB 2022-23 Series I", quantity=D(25),
                                       current_value=D(380000)))
        self.db.commit()
        res = ev.scan(self.db, ["wife"], TODAY)
        self.assertEqual(res["found_not_held"], [])
        self.assertEqual(res["held_by_hand"], [{"owner": "wife", "isin": I_22, "units": 25.0}])

    # ── a bond held by hand: identified, valued, counted ──
    def _by_hand(self, owner="wife", notes="", qty=25, name="SGB 2022-23 Series I", invested=127275):
        a = self.m.ManualAsset(owner_id=owner, asset_type="sgb", name=name, institution="RBI", quantity=D(qty), current_value=D(1),
                               invested_amount=D(invested), start_date=date(2022, 6, 28), maturity_date=date(2030, 6, 28), notes=notes)
        self.db.add(a); self.db.commit(); return a

    def _market(self):
        """Two SGBs a depository values at their market prices, as the statements do."""
        for owner, isin, per_g in (("user", III_22, 15083), ("wife", I_23, 15544)):
            acc = self.m.Account(owner_id=owner, institution="Zerodha", account_type="stocks", account_ref="z" + owner)
            self.db.add(acc); self.db.flush()
            self.db.add(self.m.Holding(account_id=acc.id, snapshot_date=TODAY, isin=isin, instrument_name="SGB", holding_type="bonds",
                                       units=D(25), current_value=D(25 * per_g), source_file="ledger:rollforward"))
        self.db.commit()

    def test_a_note_that_names_another_series_does_not_make_a_second_bond(self):          # the shipped bug
        self._by_hand(notes="Value estimated at the statement price of 2022-23 Series III (₹15,083/g)")
        self.assertEqual(ev.manual_units(self.db, ["wife"]), {("wife", I_22): D(25)})

    def test_the_isin_in_a_note_identifies_a_bond_whose_name_does_not(self):
        self._by_hand(name="Gold bond at the bank", notes=f"ISIN {III_22}")
        self.assertEqual(list(ev.manual_units(self.db, ["wife"])), [("wife", III_22)])

    def test_a_hand_held_bond_is_valued_like_the_held_sgb_with_the_nearest_maturity(self):
        self._market()
        a = self._by_hand()
        out = ev.value_hand_held(self.db); self.db.commit(); self.db.refresh(a)
        self.assertEqual(a.current_value, D(25 * 15083))                    # Jun 2030 is nearer Dec 2030 than Jun 2031
        self.assertEqual(out[0]["priced_like"], "2022-23 Series III")
        self.assertIn("Value estimated at the statement price", a.notes)
        ev.value_hand_held(self.db)                                           # idempotent: the note is not appended twice
        self.assertEqual(a.notes.count("Value estimated at"), 1)

    def test_nothing_to_compare_with_means_the_value_is_left_alone(self):
        a = self._by_hand()
        self.assertEqual(ev.value_hand_held(self.db), [])
        self.assertEqual(a.current_value, D(1))

    def test_the_gold_spot_refresh_leaves_a_known_tranche_alone(self):
        import inspect
        from app.api.routes import manual
        self.assertIn("manual_tranche(asset)", inspect.getsource(manual.refresh_metal_prices))

    def test_coupons_of_a_hand_held_bond_are_return_flows_and_its_cost_is_the_outflow(self):
        from app.engine.portfolio_xirr import _manual_asset_cashflows
        self.coupons("wife", I_22, desc="NEFT*RBIS0MBPA04*U1*Sovereign Gold B", shift=2); self.coverage("wife")
        a = self._by_hand(); a.current_value = D(377075); self.db.commit()
        cfs, invested, _cur = _manual_asset_cashflows(self.db, a, TODAY)
        self.assertEqual(cfs[0], (date(2022, 6, 28), -127275.0))
        coupons = [c for c in cfs[1:-1]]
        self.assertEqual(len(coupons), 8)
        self.assertTrue(all(amt > 1590 for _d, amt in coupons))
        self.assertEqual(cfs[-1], (TODAY, 377075.0))

    def test_the_manual_assets_page_and_the_investments_page_share_those_flows(self):
        import inspect
        from app.api.routes import manual
        self.assertIn("_manual_asset_cashflows(object_session(a), a, today)", inspect.getsource(manual._compute_returns))

    def test_deadlines_list_the_coupon_and_the_redemption_of_a_hand_held_bond(self):
        from app.engine import deadlines
        self._by_hand()
        out = deadlines.build(self.db, today=TODAY, horizon_days=1830)["items"]
        mine = [i for i in out if i["kind"] in ("sgb_coupon", "sgb_maturity") and i["owner"] == "wife"]
        self.assertEqual({(i["kind"], i["date"]) for i in mine}, {("sgb_coupon", "2026-12-28"), ("sgb_maturity", "2030-06-28")})

    def test_the_api_reports_where_the_hand_held_cost_comes_from(self):
        from app.api.routes.sgb import summary
        self.coupons("wife", I_22, desc="NEFT*RBIS0MBPA04*U1*Sovereign Gold B", shift=2); self.coverage("wife")
        self._by_hand()
        (s,) = [x for x in summary(self.db, TODAY)["streams"] if x["isin"] == I_22]
        self.assertTrue(s["held"] and s["held_by_hand"])
        self.assertIn("Manual Asset", s["cost"]["source"])
        self.assertEqual(s["cost"]["total"], 127275.0)
        self.assertEqual(s["maturity"], "2030-06-28")

    def test_series_names_are_not_confused_with_longer_ones(self):
        self.db.add(self.m.ManualAsset(owner_id="wife", asset_type="sgb", name="SGB 2022-23 Series III", quantity=D(10), current_value=D(1)))
        self.db.commit()
        self.assertEqual(ev.manual_units(self.db, ["wife"]), {("wife", III_22): D(10)})          # not also Series I

    def test_the_right_sum_in_the_wrong_season_is_not_a_coupon(self):
        self.held("user", III_22)
        self.txn("user", date(2025, 3, 15), sgb.tranche(III_22).coupon(D(25)), "credit", "NEFT*ICIC0099999*CMS1*CENTRAL DEPOSITO")
        self.coverage("user")
        (s,) = ev.scan(self.db, ["user"], TODAY)["streams"]
        self.assertEqual(s["coupons"], [])

    # ── the subscription: what was actually paid ──
    def test_the_subscription_debit_proves_the_online_price(self):
        self.held("user", III_22)
        self.txn("user", date(2022, 12, 19), 133975, "debit", "SBISG223531122052R1542111 SGBS 25 GMS", category="investment")
        paid = ev.paid_terms(self.db, "user")
        self.assertEqual(paid[III_22], (date(2022, 12, 19), D(5359), D(25)))

    def test_a_debit_that_is_not_units_times_a_published_price_is_not_taken(self):
        self.held("user", III_22)
        self.txn("user", date(2022, 12, 19), 133000, "debit", "SGBS 25 GMS")
        self.assertEqual(ev.paid_terms(self.db, "user"), {})

    def test_the_ledger_costs_the_units_at_the_price_paid(self):
        from app.ledger.engine import compute_owner
        self.held("user", III_22)
        self.txn("user", date(2022, 12, 19), 133975, "debit", "SBISG2235 SGBS 25 GMS", category="investment")
        st = compute_owner(self.db, "user").states[III_22]
        self.assertEqual(st.position.invested, D(133975))
        self.assertEqual(st.position.first_buy_date, date(2022, 12, 19))

    # ── tagging makes the evidence count in the one returns model ──
    def test_tagging_makes_coupons_income_and_isin_tags_only_held_bonds(self):
        self.held("user", III_22); self.coupons("user", III_22); self.coverage("user")
        self.coupons("wife", I_22, desc="NEFT*RBIS0MBPA04*U1*Sovereign Gold B"); self.coverage("wife")
        ch = ev.tag(self.db, TODAY)
        self.assertEqual(ch["coupons"], 7 + 8)                                                  # Series III: 7 coupons; Series I 2022-23: 8
        q = self.db.query(self.m.Transaction)
        mine = q.filter(self.m.Transaction.subcategory == "sgb_coupon", self.m.Transaction.account_id == self.bank["user"].id).all()
        self.assertTrue(all((t.category, t.isin, t.category_source) == ("income", III_22, "trade") for t in mine))
        hers = q.filter(self.m.Transaction.subcategory == "sgb_coupon", self.m.Transaction.account_id == self.bank["wife"].id).all()
        self.assertTrue(hers and all(t.category == "income" and t.isin is None for t in hers))      # income, but not in returns

    def test_tagging_the_subscription_gives_the_debit_its_isin_and_is_idempotent(self):
        self.held("user", III_22)
        sub = self.txn("user", date(2022, 12, 19), 133975, "debit", "SGBS 25 GMS", category="investment")
        self.assertEqual(ev.tag(self.db, TODAY)["subscriptions"], 1)
        self.db.refresh(sub)
        self.assertEqual((sub.isin, sub.category, sub.subcategory), (III_22, "investment", "sgb_gold"))
        self.assertEqual(ev.tag(self.db, TODAY), {"coupons": 0, "subscriptions": 0})

    def test_a_label_you_gave_is_never_overwritten(self):
        self.held("user", III_22)
        t = self.txn("user", date(2026, 6, 28), sgb.tranche(III_22).coupon(D(25)), "credit", "NEFT*ICIC0099999*CMS1*CENTRAL DEPOSITO",
                     category="gift", source="user")
        self.coverage("user")
        ev.tag(self.db, TODAY)
        self.db.refresh(t)
        self.assertEqual((t.category, t.category_source), ("gift", "user"))

    def test_the_retag_authority_runs_the_tagger(self):
        import inspect
        from app.txn import pipeline
        self.assertIn("sgb_evidence.tag(db)", inspect.getsource(pipeline.retag))

    # ── the check and the API ──
    def test_the_check_is_quiet_for_a_complete_matched_stream(self):
        from app import consistency
        self.held("user", III_22); self.coupons("user", III_22); self.coverage("user")
        with mock.patch("app.ledger.sgb_evidence.date") as d:
            d.today.return_value = TODAY
            c = consistency.sgb_coupons_reconcile(self.db)
        self.assertTrue(c["ok"], c["detail"])

    def test_the_check_names_a_bond_the_statements_do_not_list(self):
        from app import consistency
        self.coupons("wife", I_22, desc="NEFT*RBIS0MBPA04*U1*Sovereign Gold B"); self.coverage("wife")
        c = consistency.sgb_coupons_reconcile(self.db)
        self.assertFalse(c["ok"])
        self.assertIn("2022-23 Series I", c["detail"])
        self.assertIn("in no statement or Manual Asset", c["detail"])

    def test_the_api_says_where_the_cost_came_from(self):
        from app.api.routes.sgb import summary
        self.held("user", III_22); self.held("user", VII)
        self.txn("user", date(2022, 12, 19), 133975, "debit", "SGBS 25 GMS", category="investment")
        out = {s["isin"]: s for s in summary(self.db, TODAY)["streams"]}
        self.assertEqual(out[III_22]["cost"]["source"], "bank subscription debit")
        self.assertEqual(out[III_22]["cost"]["total"], 133975.0)
        self.assertIn("online price assumed", out[VII]["cost"]["source"])
        self.assertEqual(out[VII]["cost"]["total"], 25 * 4711.0)

    def test_the_check_is_registered(self):
        from app import consistency
        self.assertIn(consistency.sgb_coupons_reconcile, consistency.CHECKS)


if __name__ == "__main__":
    unittest.main()
