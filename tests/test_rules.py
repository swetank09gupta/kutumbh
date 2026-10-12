"""The public-rules registry: complete, dated, non-overlapping, looked up by date, kept fresh, and actually used."""
import re
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.knowledge import rules

APP = Path(__file__).resolve().parents[1] / "app"


class RegistryShape(unittest.TestCase):
    def test_every_entry_has_source_and_dates(self):
        for rid, entries in rules.load().items():
            for r in entries:
                self.assertRegex(r.source, r"^https?://", rid)
                self.assertIsInstance(r.effective_from, date, rid)
                self.assertIsInstance(r.verified_on, date, rid)
                self.assertGreaterEqual(r.verified_on, r.effective_from, f"{rid}: verified before it took effect")
                self.assertLessEqual(r.verified_on, date.today(), f"{rid}: verified in the future")
                self.assertTrue(r.unit, rid)
                self.assertGreater(r.value, 0, rid)

    def test_effective_ranges_do_not_overlap(self):
        for rid, entries in rules.load().items():
            starts = [r.effective_from for r in entries]
            self.assertEqual(len(starts), len(set(starts)), f"{rid}: two entries start on the same day")

    def test_file_has_no_unknown_duplicates(self):
        text = rules.PATH.read_text()
        n_entries = len(re.findall(r"^\s*- id:", text, re.M))
        self.assertEqual(n_entries, sum(len(v) for v in rules.load().values()))


class Lookup(unittest.TestCase):
    def test_threshold_changes_on_the_effective_day(self):
        self.assertEqual(rules.value("tds.194a.threshold", date(2025, 3, 31)), Decimal("40000"))
        self.assertEqual(rules.value("tds.194a.threshold", date(2025, 4, 1)), Decimal("50000"))
        self.assertEqual(rules.value("tds.194a.threshold_senior", date(2025, 4, 1)), Decimal("100000"))

    def test_capital_gains_budget_2024(self):
        before, after = date(2024, 7, 22), date(2024, 7, 23)
        self.assertEqual((rules.value("cg.equity.stcg_rate", before), rules.value("cg.equity.stcg_rate", after)),
                         (Decimal("15"), Decimal("20")))
        self.assertEqual((rules.value("cg.equity.ltcg_rate", before), rules.value("cg.equity.ltcg_rate", after)),
                         (Decimal("10"), Decimal("12.5")))
        self.assertEqual((rules.value("cg.equity.ltcg_exemption", before), rules.value("cg.equity.ltcg_exemption", after)),
                         (Decimal("100000"), Decimal("125000")))

    def test_no_guessing_before_the_first_verified_entry(self):
        with self.assertRaises(rules.RuleNotInForce):
            rules.value("ppf.annual_limit", date(2010, 1, 1))
        with self.assertRaises(rules.RuleNotInForce):
            rules.value("no.such.rule")

    def test_value_is_exact_decimal(self):
        self.assertEqual(rules.value("cg.equity.ltcg_rate", date(2026, 1, 1)), Decimal("12.5"))
        self.assertIsInstance(rules.value("mf.stamp_duty"), Decimal)


class Staleness(unittest.TestCase):
    def test_fresh_within_the_quarter(self):
        self.assertEqual(rules.stale(date(2026, 12, 31)), [])

    def test_quarter_start_makes_everything_stale(self):
        stale = {r["id"] for r in rules.stale(date(2027, 1, 1))}
        self.assertEqual(stale, set(rules.load()))

    def test_old_verification_is_stale_even_inside_a_quarter(self):
        r = rules.rule("ppf.annual_limit")
        self.assertTrue(rules.STALE_AFTER_DAYS < 366)
        late = date(r.verified_on.year + 1, r.verified_on.month, 1)
        self.assertIn("ppf.annual_limit", {x["id"] for x in rules.stale(late)})


class CodeUsesTheRegistry(unittest.TestCase):
    """Mutation guards: these fail if a public number is typed back into the code."""

    def test_limits_are_not_hardcoded(self):
        src = (APP / "engine" / "recommendations.py").read_text()
        self.assertNotIn('Decimal("150000")', src)
        self.assertNotIn("₹1,50,000", src)
        self.assertIn('rules.value("ppf.annual_limit")', src)
        self.assertIn('rules.value("income_tax.80c_cap")', src)

    def test_ppf_default_rate_is_not_typed_in(self):
        self.assertNotIn('Decimal("7.1")', (APP / "engine" / "ppf_from_transfers.py").read_text())

    def test_staleness_check_is_registered(self):
        from app.consistency import CHECKS, rules_are_current
        self.assertIn(rules_are_current, CHECKS)


if __name__ == "__main__":
    unittest.main()
