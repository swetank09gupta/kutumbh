"""Decision (2026-10): no number is invented. A missing purchase or sale is recovered from evidence or reported as
missing — never filled with an assumption such as a break-even redemption."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


class NoInventedData(unittest.TestCase):
    def test_nothing_creates_synthetic_redemptions(self):
        creators = []
        for p in list(APP.rglob("*.py")) + list((ROOT / "scripts").glob("*.py")):
            text = p.read_text(encoding="utf-8")
            if "[SYNTH]" in text or re.search(r"reference_number\s*=\s*.*SYNTH", text):
                creators.append(str(p.relative_to(ROOT)))
        self.assertEqual(creators, [], "nothing may write a [SYNTH] row; only the purge of old ones may mention SYNTH:")

    def test_the_purge_is_the_only_synth_reference(self):
        hits = [p.name for p in APP.rglob("*.py") if "SYNTH:" in p.read_text(encoding="utf-8")]
        self.assertEqual(hits, ["maintenance.py"])


if __name__ == "__main__":
    unittest.main()
