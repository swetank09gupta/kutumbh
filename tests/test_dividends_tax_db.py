"""
Dividends proven by arithmetic, and the tax view's income / TDS side, on a scratch database. Synthetic numbers shaped like the
real credits ("ACHCr HDFC… TATASTEELDIV 2026", truncated payer names, one purchase made on the ex-date itself).
Needs a scratch Postgres (TEST_DATABASE_URL).
"""
import json
import os
import tempfile
import unittest
import uuid
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from app.ledger import dividends

URL = os.getenv("TEST_DATABASE_URL")
TODAY = date(2026, 10, 12)
ACME, WIDGET, GADGET = "INE000A01010", "INE000B01010", "INE000C01010"


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class World(unittest.TestCase):
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
            c.execute(text("TRUNCATE transactions, instrument_trades, tds_deductions, accounts, owners CASCADE"))
        self.db, m = self.S(), self.m
        self.db.add(m.Owner(id="user", name="u", tax_regime="old"))
        self.db.add(m.Owner(id="wife", name="w", tax_regime="new"))
        self.db.flush()
        self.bank = {o: m.Account(owner_id=o, institution="SBI", account_type="bank", account_ref=o) for o in ("user", "wife")}
        self.zer = m.Account(owner_id="user", institution="Zerodha", account_type="stocks", account_ref="z")
        self.db.add_all([*self.bank.values(), self.zer]); self.db.commit()
        self.tmp = Path(tempfile.mkdtemp()) / "dividends.json"
        p = mock.patch.object(dividends, "CACHE", self.tmp); p.start(); self.addCleanup(p.stop)
        self.addCleanup(self.db.close)
        self.history({"ACME": [("2025-06-06", "3.6")], "WIDGET": [("2025-06-10", "2.5")], "GADGET": [("2025-06-12", "3.5625")]})

    # ── builders ──
    def history(self, h):
        self.tmp.write_text(json.dumps({s: {"at": TODAY.isoformat(), "rows": rows} for s, rows in h.items()}))

    def buy(self, isin, symbol, day, qty, owner="user", side="B"):
        self.db.add(self.m.InstrumentTrade(owner_id=owner, account_id=self.zer.id, isin=isin, symbol=symbol, instrument_name=symbol,
                                           holding_type="equity", trade_date=day, side=side, quantity=D(qty), price=D(100), amount=D(100) * D(qty),
                                           source="contract_note", natural_key=uuid.uuid4().hex))
        self.db.commit()

    def credit(self, day, amount, narration, owner="user", **kw):
        t = self.m.Transaction(account_id=self.bank[owner].id, date=day, amount=D(str(amount)), transaction_type=kw.get("kind", "credit"),
                               description=narration, raw_description=narration, fingerprint=uuid.uuid4().hex,
                               category=kw.get("category", "other"), subcategory=kw.get("sub"), category_source="rule")
        self.db.add(t); self.db.commit(); return t

    def standard(self):
        self.buy(ACME, "ACME", date(2025, 1, 10), 100)
        self.buy(ACME, "ACME-A", date(2025, 6, 6), 50)                  # bought ON the ex-date: not entitled
        self.buy(WIDGET, "WIDGET-A", date(2025, 2, 1), 57)
        self.buy(GADGET, "GADGET", date(2025, 2, 1), 40)

    # ── matching ──
    def test_a_credit_is_a_dividend_when_shares_held_before_the_ex_date_times_the_amount_fits(self):
        self.standard()
        self.credit(date(2025, 7, 4), "360.00", "ACHCr HDFC00678000009461 ACMEDIV 2026")
        r = dividends.scan(self.db, TODAY)
        (m,) = r["matched"]
        self.assertEqual((m["symbol"], m["units"], m["dps"], m["kind"], m["ex_date"]), ("ACME", 100.0, 3.6, "gross", "2025-06-06"))

    def test_shares_bought_on_the_ex_date_are_not_entitled(self):
        self.standard()
        self.credit(date(2025, 7, 4), "540.00", "ACHCr HDFC00678000009461 ACMEDIV 2026")            # 150 x 3.6 would need the ex-date purchase
        r = dividends.scan(self.db, TODAY)
        self.assertEqual((r["matched"], len(r["unmatched"])), ([], 1))

    def test_after_tds_the_credit_is_ninety_percent(self):
        self.standard()
        self.credit(date(2025, 7, 4), "324.00", "ACHCr HDFC00678000009461 ACMEDIV 2026")
        (m,) = dividends.scan(self.db, TODAY)["matched"]
        self.assertEqual(m["kind"], "after_tds")

    def test_the_narration_separates_two_stocks_that_fit_the_same_sum(self):
        self.standard()                                                     # 57 x 2.5 = 142.5 = 40 x 3.5625
        self.credit(date(2025, 7, 10), "142.50", "ACHCr HDFC01383000011320 WIDGETFOOTWEAR")
        (m,) = dividends.scan(self.db, TODAY)["matched"]
        self.assertEqual(m["symbol"], "WIDGET")

    def test_two_stocks_that_fit_and_a_silent_narration_are_ambiguous_not_guessed(self):
        self.standard()
        self.credit(date(2025, 7, 10), "142.50", "ACHCr HDFC01383000011320 UNKNOWN PAYER")
        r = dividends.scan(self.db, TODAY)
        self.assertEqual(r["matched"], [])
        self.assertEqual(r["ambiguous"][0]["could_be"], ["GADGET", "WIDGET"])

    def test_a_credit_long_after_the_ex_date_or_of_the_wrong_sum_is_unmatched(self):
        self.standard()
        self.credit(date(2025, 10, 20), "360.00", "ACHCr HDFC00678000009461 ACMEDIV")               # 136 days after the ex-date
        self.credit(date(2025, 7, 4), "361.00", "ACHCr HDFC00678000009461 ACMEDIV")                 # a rupee off
        r = dividends.scan(self.db, TODAY)
        self.assertEqual((r["matched"], len(r["unmatched"])), ([], 2))

    def test_salary_and_sgb_coupons_are_not_candidates(self):
        self.standard()
        self.credit(date(2025, 7, 4), "360.00", "SALARY CREDIT")
        self.credit(date(2025, 7, 4), "360.00", "NEFT*ICIC0099999*CMS1*CENTRAL DEPOSITO")
        r = dividends.scan(self.db, TODAY)
        self.assertEqual((r["matched"], r["unmatched"], r["ambiguous"]), ([], [], []))

    def test_each_owner_is_matched_on_their_own_shares(self):
        self.standard()
        self.buy(ACME, "ACME", date(2025, 1, 10), 80, owner="wife")
        self.credit(date(2025, 7, 4), "288.00", "ACHCr HDFC00678000009461 ACMEDIV 2026", owner="wife")      # 80 x 3.6
        (m,) = dividends.scan(self.db, TODAY)["matched"]
        self.assertEqual((m["owner"], m["units"]), ("wife", 80.0))

    def test_a_final_dividend_credited_84_days_after_the_ex_date_is_matched(self):
        self.standard()
        self.credit(date(2025, 8, 29), "360.00", "ACHCr HDFC00678000009461 ACMEDIV 2026")             # 84 days after 6 Jun
        self.assertEqual(len(dividends.scan(self.db, TODAY)["matched"]), 1)

    def test_the_amount_paid_per_old_share_is_found_when_a_later_bonus_adjusted_the_history(self):
        self.standard()                                                 # Yahoo shows 1.8 per share after a 1:1 bonus (2:1) in Dec 2025;
        self.tmp.write_text(json.dumps({"ACME": {"at": TODAY.isoformat(), "rows": [["2025-06-06", "1.8"]],
                                                 "splits": [["2025-12-01", "2", "1"]]}}))      # the bank paid 3.6 on each of the 100 shares
        self.credit(date(2025, 7, 4), "360.00", "ACHCr HDFC00678000009461 ACMEDIV 2026")
        (m,) = dividends.scan(self.db, TODAY)["matched"]
        self.assertEqual((m["dps"], m["units"]), (3.6, 100.0))

    def test_a_registrar_that_pays_whole_rupees_is_matched(self):
        self.standard()
        self.credit(date(2025, 7, 10), "142.00", "ACHCr HDFC01383000011320 WIDGETFOOTWEAR")          # 57 x 2.5 = 142.50 paid down to the rupee
        (m,) = dividends.scan(self.db, TODAY)["matched"]
        self.assertEqual((m["symbol"], m["kind"]), ("WIDGET", "gross_rounded_down"))

    def test_shares_held_before_our_history_are_matched_on_the_name_and_the_arithmetic_and_say_so(self):
        self.standard()
        self.credit(date(2025, 7, 4), "540.00", "ACHCr HDFC00678000009461 ACMEDIV 2026", owner="wife")   # wife has no ACME trades: 150 x 3.6
        (m,) = dividends.scan(self.db, TODAY)["matched"]
        self.assertEqual((m["symbol"], m["units"]), ("ACME", 150.0))
        self.assertIn("before our trade history", m["units_from"])

    def test_the_name_and_arithmetic_fallback_never_overrides_what_our_trades_say(self):
        self.standard()                                                 # user held 100 ACME: 540 = 150 x 3.6 would need shares he did not have
        self.credit(date(2025, 7, 4), "540.00", "ACHCr HDFC00678000009461 ACMEDIV 2026")
        self.assertEqual(dividends.scan(self.db, TODAY)["matched"], [])

    def test_a_renamed_ticker_is_looked_up_under_its_new_name(self):
        resp = mock.Mock(); resp.json.return_value = {"chart": {"result": [{"events": {}}]}}
        with mock.patch.object(dividends.httpx, "get", return_value=resp) as g:
            dividends.fetch("TATAMOTORS")
        self.assertIn("/TMPV.NS", g.call_args[0][0])

    # ── tagging ──
    def test_tagging_makes_a_dividend_income_and_a_flow_of_that_stock_and_is_idempotent(self):
        self.standard()
        t = self.credit(date(2025, 7, 4), "360.00", "ACHCr HDFC00678000009461 ACMEDIV 2026")
        self.assertEqual(dividends.tag(self.db, TODAY), 1)
        self.db.refresh(t)
        self.assertEqual((t.category, t.subcategory, t.isin, t.category_source), ("income", "dividend", ACME, "trade"))
        self.assertEqual(dividends.tag(self.db, TODAY), 0)

    def test_a_label_you_gave_is_never_overwritten(self):
        self.standard()
        t = self.credit(date(2025, 7, 4), "360.00", "ACHCr HDFC00678000009461 ACMEDIV 2026")
        t.category, t.category_source = "gift", "user"; self.db.commit()
        dividends.tag(self.db, TODAY); self.db.refresh(t)
        self.assertEqual((t.category, t.category_source), ("gift", "user"))

    def test_tagging_never_touches_the_network(self):
        self.standard(); self.credit(date(2025, 7, 4), "360.00", "ACHCr HDFC00678000009461 ACMEDIV 2026")
        with mock.patch.object(dividends, "fetch", side_effect=AssertionError("network")):
            dividends.tag(self.db, TODAY)

    # ── the cache ──
    def test_a_failed_fetch_keeps_the_cached_history(self):
        self.standard()
        self.history({"ACME": [("2025-06-06", "3.6")]})
        self.tmp.write_text(json.dumps({"ACME": {"at": "2026-01-01", "rows": [["2025-06-06", "3.6"]]}}))      # stale cache
        out = dividends.refresh_history(self.db, fetcher=mock.Mock(side_effect=OSError("offline")), today=TODAY)
        self.assertEqual(out["failed"], 3)                                                                   # ACME, WIDGET, GADGET
        self.assertEqual(dividends.history("ACME"), [(date(2025, 6, 6), D("3.6"))])

    def test_a_fresh_cache_is_not_fetched_again(self):
        self.standard()
        fetcher = mock.Mock(return_value=[(date(2025, 6, 6), D("3.6"))])
        dividends.refresh_history(self.db, fetcher=fetcher, today=TODAY)
        first = fetcher.call_count
        dividends.refresh_history(self.db, fetcher=fetcher, today=TODAY)
        self.assertEqual(fetcher.call_count, first)

    # ── the tax view: income, TDS, regime ──
    def _income(self, owner="user", today=TODAY):
        from app.engine import tax_view
        o = self.db.get(self.m.Owner, owner)
        return {i["fy"]: i for i in tax_view.income(self.db, o, today)}

    def test_interest_is_split_by_type_and_by_financial_year(self):
        self.credit(date(2026, 3, 25), 1000, "INTEREST CREDIT", category="income", sub="interest_dividend")
        self.credit(date(2026, 4, 5), 500, "INTEREST CREDIT", category="income", sub="interest_dividend")
        self.credit(date(2026, 3, 31), 4000, "INT TRF FRM 43960134737 TO 30778223316", category="income", sub="interest_dividend")
        self.credit(date(2026, 4, 1), 2000, "INT TRF FRM 43960134737 TO 30778223316", category="income", sub="interest_dividend")
        old, new = self._income()["2025-26"], self._income()["2026-27"]
        self.assertEqual((old["savings_interest"], old["deposit_interest_credited"]), (1000.0, 4000.0))
        self.assertEqual((new["savings_interest"], new["deposit_interest_credited"]), (500.0, 2000.0))      # 1 April belongs to the new year

    def test_tds_is_added_back_and_a_threshold_crossed_by_the_notice_alone_is_flagged(self):
        self.credit(date(2026, 2, 10), 45000, "INT TRF FRM 43960134737 TO 30778223316", category="income", sub="interest_dividend")
        self.db.add(self.m.TdsDeduction(owner_id="user", account_last4="4737", deducted_on=date(2026, 3, 11), amount=D(500), natural_key="t1"))
        self.db.commit()
        p = self._income()["2025-26"]["tds_by_payer"][0]
        self.assertEqual((p["interest_credited"], p["tds_deducted"], p["gross_interest"]), (45000.0, 500.0, 45500.0))
        self.assertTrue(p["over_threshold"] and p["crossed_by_tds"])                  # 45,500 < 50,000, yet the bank deducted: accruals not credited
        self.assertEqual(p["threshold"], 50000.0)

    def test_a_payer_over_the_threshold_without_a_notice(self):
        self.credit(date(2026, 2, 10), 52000, "INT TRF FRM 43960134737 TO 30778223316", category="income", sub="interest_dividend")
        p = self._income()["2025-26"]["tds_by_payer"][0]
        self.assertTrue(p["over_threshold"]); self.assertFalse(p["crossed_by_tds"])

    def test_the_threshold_of_each_year_is_the_one_in_force_then(self):
        self.credit(date(2024, 8, 10), 10, "INT TRF FRM 43960134737 TO 30778223316", category="income", sub="interest_dividend")
        self.assertEqual(self._income()["2024-25"]["tds_by_payer"][0]["threshold"], 40000.0)

    def test_80tta_uses_the_cap_of_that_year_and_none_before_the_rule_existed(self):
        self.credit(date(2011, 8, 10), 14000, "INTEREST CREDIT", category="income", sub="interest_dividend")
        self.credit(date(2025, 8, 10), 14000, "INTEREST CREDIT", category="income", sub="interest_dividend")
        self.assertEqual(self._income(today=date(2026, 10, 12))["2025-26"]["deduction_80tta"], 10000.0)      # the year you file for
        self.assertIsNone(self._income(today=date(2012, 1, 1))["2011-12"]["deduction_80tta"])                # before FY 2012-13: not applicable

    def test_a_year_before_the_first_verified_threshold_makes_no_check(self):
        self.credit(date(2019, 2, 10), 60000, "INT TRF FRM 43960134737 TO 30778223316", category="income", sub="interest_dividend")
        p = self._income(today=date(2019, 10, 1))["2018-19"]["tds_by_payer"][0]
        self.assertEqual((p["threshold"], p["over_threshold"], p["headroom"]), (None, False, None))        # never an invented threshold

    def test_80tta_is_for_the_old_regime_only(self):
        for o in ("user", "wife"):
            self.credit(date(2026, 1, 10), 14000, "INTEREST CREDIT", owner=o, category="income", sub="interest_dividend")
        self.assertEqual(self._income("user")["2025-26"]["deduction_80tta"], 10000.0)         # capped
        self.assertEqual(self._income("wife")["2025-26"]["deduction_80tta"], 0.0)             # new regime: none

    def test_dividends_count_from_the_proof_even_before_they_are_tagged(self):
        self.standard()
        self.credit(date(2025, 7, 4), "360.00", "ACHCr HDFC00678000009461 ACMEDIV 2026")
        self.credit(date(2025, 8, 4), "999.00", "ACHCr XX UNKNOWN LTD")
        i = self._income()["2025-26"]
        self.assertEqual((i["dividends_proven"], i["dividends_unmatched"], i["dividends_unmatched_count"]), (360.0, 999.0, 1))
        self.assertEqual(i["investment_income_at_slab"], 1359.0)
        self.assertEqual(i["dividend_by_company"][0]["company"], "ACME")

    def test_sgb_coupons_are_investment_income(self):
        self.credit(date(2026, 6, 28), 1690.31, "NEFT*ICIC0099999*CMS1*CENTRAL DEPOSITO", category="income", sub="sgb_coupon")
        self.assertEqual(self._income()["2026-27"]["sgb_coupons"], 1690.31)

    def test_a_debit_labelled_interest_income_is_reported_not_trusted(self):
        from app.engine import tax_view
        self.credit(date(2020, 7, 9), 100000, "BRN-SI-D1-TRFR FROM 09-07-2020 Summary of Deposits Interest Earned",
                    owner="wife", kind="debit", category="income", sub="interest_dividend")
        issues = tax_view._issues(self.db, ["user", "wife"])
        self.assertEqual([i["kind"] for i in issues], ["debit_labelled_interest_income"])
        self.assertEqual(issues[0]["rows"][0]["amount"], 100000.0)
        self.assertEqual(self._income("wife", today=date(2021, 3, 1))["2020-21"]["investment_income_at_slab"], 0.0)   # and not counted as income

    # ── the indicative tax estimate ──
    def salary(self, owner, day, amount):
        self.credit(day, amount, "NEFT SALARY", owner=owner, category="income", sub="salary")

    def est(self, owner, fy="2025-26", claims=None, today=TODAY):
        from app.engine import tax_view
        o = self.db.get(self.m.Owner, owner)
        inc = {i["fy"]: i for i in tax_view.income(self.db, o, today)}
        return tax_view.estimate(self.db, o, fy, inc[fy], None, claims or {}, today)

    def test_new_regime_salary_gets_the_standard_deduction_and_slab_tax(self):
        for k in range(12):
            self.salary("wife", date(2025, 4 + k % 9, 28) if k < 9 else date(2026, k - 8, 28), 100000)       # 12 months of 1 lakh
        e = self.est("wife")
        self.assertEqual((e["salary_credited"], e["salary_months_seen"]), (1200000.0, 12))
        self.assertEqual(e["deductions"], {"standard_deduction": 75000.0})
        self.assertEqual(e["taxable_ordinary_income"], 1125000.0)                      # 12.0 L - 75,000
        self.assertEqual(e["indicative_total"], 0.0)                                   # under the 12 L rebate limit
        self.assertTrue(any("floor" in c for c in e["caveats"]))

    def test_old_regime_takes_the_home_loan_claims_and_80tta_and_caps_80c(self):
        for k in range(12):
            self.salary("user", date(2025, 4 + k % 9, 28) if k < 9 else date(2026, k - 8, 28), 300000)       # 36 L credited
        self.credit(date(2026, 1, 10), 14000, "INTEREST CREDIT", category="income", sub="interest_dividend")
        claims = {"2025-26": [{"owner": "user", "claim_24b": 200000.0, "principal_for_80c": 400000.0}]}
        e = self.est("user", claims=claims)
        self.assertEqual(e["deductions"], {"standard_deduction": 50000.0, "home_loan_interest_24b": 200000.0, "section_80c": 150000.0,
                                           "savings_interest_80tta": 10000.0})            # 80C capped at 1.5 L, 80TTA at 10,000
        self.assertEqual(e["taxable_ordinary_income"], 3600000 + 14000 - 50000 - 200000 - 150000 - 10000)

    def test_the_new_regime_ignores_the_home_loan_and_80c(self):
        self.salary("wife", date(2025, 6, 28), 2000000)
        claims = {"2025-26": [{"owner": "wife", "claim_24b": 0.0, "principal_for_80c": 400000.0}]}
        self.assertEqual(set(self.est("wife", claims=claims)["deductions"]), {"standard_deduction"})

    def test_missing_months_and_an_unfinished_year_are_said(self):
        self.salary("wife", date(2025, 6, 28), 50000)
        e = self.est("wife")
        self.assertTrue(any("only 1 month" in c for c in e["caveats"]))
        cur = self.est("wife", fy="2026-27")
        self.assertTrue(cur["partial_year"] and any("not over" in c for c in cur["caveats"]))

    def test_a_year_with_no_slab_table_gets_no_estimate(self):
        self.assertIsNone(self.est("wife", fy="2023-24", today=date(2024, 6, 1)))

    def test_special_rate_tax_on_gains_is_added_with_cess(self):
        from app.engine import tax_view
        o = self.db.get(self.m.Owner, "wife")
        inc = {i["fy"]: i for i in tax_view.income(self.db, o, TODAY)}
        gains = {"estimated_tax": 1000.0, "debt_funds_at_slab": 0.0, "gold_silver_etf": {"stcg_at_slab": 0.0}}
        e = tax_view.estimate(self.db, o, "2025-26", inc["2025-26"], gains, {}, TODAY)
        self.assertEqual((e["special_rate_tax_on_gains"], e["indicative_total"]), (1000.0, 1040.0))

    def test_the_whole_view_builds_for_both_people(self):
        from app.engine import tax_view
        self.standard()
        out = tax_view.build(self.db, TODAY)
        self.assertEqual(set(out["owners"]), {"user", "wife"})
        self.assertEqual(out["owners"]["user"]["regime"], "old")
        self.assertEqual(out["owners"]["wife"]["regime"], "new")
        self.assertEqual(out["owners"]["user"]["gains"][0]["fy"], "2026-27")


if __name__ == "__main__":
    unittest.main()
