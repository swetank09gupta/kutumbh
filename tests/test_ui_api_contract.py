"""
Every API call the single-page UI makes must hit a route that exists, with the method it uses.
(A renamed or removed endpoint otherwise shows up as a silently empty card.)
"""
import re
import unittest
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
CALL = re.compile(r"""(?:apiFetch|fetch)\(\s*([`'"])(/api/[^`'"]*)\1(.{0,260})""", re.S)


def _route_patterns():
    from app.main import app
    out = []
    for r in app.routes:
        methods = getattr(r, "methods", None)
        path = getattr(r, "path", "")
        if methods and path.startswith("/api/"):
            out.append((re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path.rstrip("/")) + "/?$"), set(methods)))
    return out


def missing_calls(html: str) -> list[str]:
    routes = _route_patterns()
    missing = []
    for m in CALL.finditer(html):
        url = re.sub(r"\$\{[^}]*\}", "x", m.group(2)).split("?")[0].rstrip("/")
        method = (re.search(r"method:\s*['\"](\w+)['\"]", m.group(3)) or [None, "GET"])[1].upper()
        if not any(p.match(url) and method in ms for p, ms in routes):
            missing.append(f"{method} {m.group(2)}")
    return sorted(set(missing))


class UiApiContract(unittest.TestCase):
    def test_every_ui_call_has_a_matching_route_and_method(self):
        self.assertEqual(missing_calls(INDEX.read_text(encoding="utf-8")), [], "UI calls an endpoint that does not exist")

    def test_it_reports_a_wrong_path_and_a_wrong_method(self):
        self.assertEqual(missing_calls("apiFetch('/api/goals/nope')"), ["GET /api/goals/nope"])
        self.assertEqual(missing_calls("apiFetch('/api/goals/inflation', { method: 'POST' })"),
                         ["POST /api/goals/inflation"])

    def test_the_scanner_finds_the_calls(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertGreater(len(CALL.findall(html)), 30)               # guards against the regex silently matching nothing


if __name__ == "__main__":
    unittest.main()
