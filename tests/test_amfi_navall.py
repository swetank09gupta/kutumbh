"""AMFI's NAVAll.txt: both layouts must give a NAV. In Oct 2026 AMFI split the name into Scheme Name;Plan;Option and moved
the NAV to column 7; the old reader took "Direct Plan" for the NAV, every fund came out at 0 and all funds silently fell back to
an old trade or statement price."""
import unittest

from app.engine.amfi_isin import _normalize, _parse_navall

NEW = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - ELSS)

Quant Mutual Fund

120847;INF966L01986;-;Quant ELSS Tax Saver Fund;Direct Plan;Growth Option;445.1864;12-Oct-2026
120846;INF966L01994;-;Quant ELSS Tax Saver Fund;Direct Plan;IDCW Option;99.5;12-Oct-2026
"""
OLD = """Scheme Code;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - ELSS)

120847;INF966L01986;-;Quant ELSS Tax Saver Fund - Growth Option - Direct Plan;445.1864;12-Oct-2025
"""


class Layouts(unittest.TestCase):
    def test_the_current_layout_gives_the_nav_and_category(self):
        a, b = _parse_navall(NEW)
        self.assertEqual((a["isin"], a["nav"], a["category"]), ("INF966L01986", 445.1864, "Equity Scheme - ELSS"))
        self.assertEqual(b["nav"], 99.5)

    def test_the_name_keeps_plan_and_option_so_name_matching_still_works(self):
        a, _ = _parse_navall(NEW)
        self.assertEqual(_normalize(a["scheme_name"]), _normalize("Quant ELSS Tax Saver Fund - Growth Option - Direct Plan"))

    def test_the_old_layout_still_reads(self):
        (a,) = _parse_navall(OLD)
        self.assertEqual((a["isin"], a["nav"]), ("INF966L01986", 445.1864))


if __name__ == "__main__":
    unittest.main()


class Resilience(unittest.TestCase):
    def setUp(self):
        from app.engine import amfi_isin as a
        self.a = a
        self.saved = (a._NAV_CACHE, a._NAV_LOADED, a._LAST_FAILURE)
        a._NAV_CACHE, a._LAST_FAILURE = None, None
        self.addCleanup(lambda: setattr(a, "_NAV_CACHE", self.saved[0]) or setattr(a, "_NAV_LOADED", self.saved[1])
                        or setattr(a, "_LAST_FAILURE", self.saved[2]))

    def test_a_failed_fetch_is_retried_not_remembered_as_no_navs(self):
        from unittest import mock
        with mock.patch.object(self.a, "_load_cache", return_value=None), \
             mock.patch.object(self.a, "_fetch_navall", side_effect=OSError("offline")):
            self.assertIsNone(self.a.get_nav("INF966L01986"))
        self.assertIsNone(self.a._NAV_CACHE)                                        # not cached as {}
        self.a._LAST_FAILURE = None                                                 # the retry window passed
        entries = [{"isin": "INF966L01986", "isin2": "", "scheme_name": "Q", "scheme_code": "1", "nav": 445.0, "category": ""}]
        with mock.patch.object(self.a, "_load_cache", return_value=None), \
             mock.patch.object(self.a, "_fetch_navall", return_value=entries), mock.patch.object(self.a, "_save_cache"):
            self.assertEqual(self.a.get_nav("INF966L01986"), 445.0)

    def test_the_in_memory_map_expires(self):
        from unittest import mock
        self.a._NAV_CACHE, self.a._NAV_LOADED = {"INF1": 1.0}, 0.0                   # loaded "long ago" on the monotonic clock
        entries = {"entries": [{"isin": "INF1", "nav": 2.0}]}
        with mock.patch.object(self.a.time, "monotonic", return_value=7 * 3600.0), mock.patch.object(self.a, "_load_cache", return_value=entries):
            self.assertEqual(self.a.get_nav("INF1"), 2.0)
