"""The deployment files: what must hold for the automatic updates to keep running and for a fresh install to recover."""
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class Compose(unittest.TestCase):
    def setUp(self):
        self.svc = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]

    def test_the_long_running_services_restart_by_themselves(self):          # the 6-hourly update lives in the app
        for name in ("postgres", "app", "grafana", "ollama"):
            self.assertEqual(self.svc[name].get("restart"), "unless-stopped", name)

    def test_one_shot_jobs_do_not_restart(self):
        for name in ("ollama-init", "bootstrap"):
            self.assertEqual(self.svc[name].get("restart"), "no", name)

    def test_the_bootstrap_waits_for_the_database_and_the_app(self):
        dep = self.svc["bootstrap"]["depends_on"]
        self.assertEqual({k: v["condition"] for k, v in dep.items()}, {"postgres": "service_healthy", "app": "service_healthy"})

    def test_the_model_is_pulled_only_after_ollama_is_healthy(self):
        self.assertEqual(self.svc["ollama-init"]["depends_on"]["ollama"]["condition"], "service_healthy")


class Bootstrap(unittest.TestCase):
    script = (ROOT / "scripts" / "docker-init.sh").read_text()

    def test_it_is_valid_shell(self):
        self.assertEqual(subprocess.run(["sh", "-n", str(ROOT / "scripts" / "docker-init.sh")]).returncode, 0)

    def test_fresh_means_no_transactions_not_few_owners(self):               # the app seeds the owners before it is healthy
        self.assertIn("FROM transactions", self.script)
        self.assertNotIn("FROM owners WHERE id NOT LIKE", self.script.split("# ── Check if backup")[0])


if __name__ == "__main__":
    unittest.main()
