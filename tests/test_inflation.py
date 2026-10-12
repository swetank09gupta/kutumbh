import unittest

from app.engine.inflation import parse_series

PAYLOAD = [{}, [{"date": str(y), "value": v} for y, v in
                [(2025, 2.4), (2024, 4.95), (2023, 5.65), (2022, 6.7), (2021, 5.13),
                 (2020, 6.62), (2019, 3.73), (2018, 3.94), (2017, 3.33), (2016, 4.95)]]]


class Cpi(unittest.TestCase):
    def test_latest_year_and_long_run_average_are_kept_apart(self):
        s = parse_series(PAYLOAD)
        self.assertEqual((s["latest"], s["latest_year"]), (2.4, "2025"))
        self.assertAlmostEqual(s["avg_10y"], 4.74, places=2)

    def test_missing_values_are_skipped(self):
        data = [{}, [{"date": "2025", "value": None}, {"date": "2024", "value": 5.0}]]
        s = parse_series(data)
        self.assertEqual((s["latest"], s["latest_year"], s["avg_10y"]), (5.0, "2024", 5.0))

    def test_empty_payload_falls_back_to_planning_default(self):
        self.assertEqual(parse_series([{}, []])["avg_10y"], 6.0)


if __name__ == "__main__":
    unittest.main()
