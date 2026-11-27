"""Cloud ranking: fixed weights, a fit floor, company-rule exclusions and byte-stable output."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import SCRIPTS, write
import ranking

SCRIPT = SCRIPTS / "solsarch.py"


class Ranking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls.tmp.name)
        write(cls.repo, "requirements.txt", "fastapi==0.110.0\npsycopg2-binary\nredis\n")
        write(cls.repo, "app/main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
        write(cls.repo, "Dockerfile", "FROM python:3.12-slim\nEXPOSE 8000\n")
        cls.env = dict(os.environ, SOLSARCH_OFFLINE="1", SOLSARCH_SKIP_VALIDATE="1", SOLSARCH_TODAY="2026-10-23", SOLSARCH_NO_HISTORY="1")
        cls.cli("scan")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def cli(cls, *args):
        done = subprocess.run([sys.executable, "-I", str(SCRIPT)] + list(args) + ["--repo", str(cls.repo)], capture_output=True, text=True, env=cls.env)
        return done.returncode, done.stdout

    def load(self):
        return json.loads((self.repo / ".solsarch" / "ranking.json").read_text(encoding="utf-8"))

    def test_every_priority_has_weights_that_sum_to_one(self):
        for name, weights in ranking.WEIGHTS.items():
            self.assertAlmostEqual(sum(weights.values()), 1.0, places=6, msg=name)
            self.assertEqual(set(weights), set(ranking.METRICS))

    def test_missing_metrics_share_their_weight_out(self):
        total, used = ranking.combine({"fit": 80, "ops": 60, "portability": 40, "reliability": 100, "cost": None}, ranking.WEIGHTS["cost"])
        self.assertNotIn("cost", used)
        self.assertAlmostEqual(sum(used.values()), 1.0, places=2)
        self.assertTrue(0 <= total <= 100)

    def test_ranking_runs_orders_by_total_and_drops_poor_fits(self):
        code, out = self.cli("rank", "--priority", "portability")
        self.assertEqual(code, 0, out)
        data = self.load()
        totals = [r["total"] for r in data["ranking"]]
        self.assertEqual(totals, sorted(totals, reverse=True))
        self.assertTrue(all(r["scores"]["fit"] >= ranking.FIT_FLOOR for r in data["ranking"]))
        self.assertTrue(any("covers only" in e["reason"] for e in data["excluded"]))
        self.assertFalse(data["cost_used"])
        self.assertIn("One cloud or several", (self.repo / "docs" / "solsarch-cloud-ranking.md").read_text(encoding="utf-8"))

    def test_priority_changes_the_weights_shown(self):
        self.cli("rank", "--priority", "cost")
        cost = self.load()["weights"]
        self.cli("rank", "--priority", "reliability")
        self.assertNotEqual(cost, self.load()["weights"])

    def test_same_input_gives_identical_bytes(self):
        self.cli("rank", "--clouds", "aws,azure,gcp")
        first = (self.repo / ".solsarch" / "ranking.json").read_bytes()
        self.cli("rank", "--clouds", "aws,azure,gcp")
        self.assertEqual(first, (self.repo / ".solsarch" / "ranking.json").read_bytes())

    def test_company_rules_exclude_a_cloud_with_the_reason(self):
        write(self.repo, ".solsarch/rules/company.yaml", "rules:\n  - kind: allowed_clouds\n    value: [aws]\n    level: required\n")
        try:
            code, out = self.cli("rank", "--clouds", "aws,azure,gcp")
            self.assertEqual(code, 0, out)
            data = self.load()
            self.assertEqual([r["cloud"] for r in data["ranking"]], ["aws"])
            self.assertEqual(sorted(e["cloud"] for e in data["excluded"]), ["azure", "gcp"])
        finally:
            (self.repo / ".solsarch" / "rules" / "company.yaml").unlink()
            (self.repo / ".solsarch" / "rules.json").unlink(missing_ok=True)

    def test_unknown_cloud_and_missing_scan_are_errors(self):
        self.assertEqual(self.cli("rank", "--clouds", "nowhere")[0], 2)
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(ranking.main(["--repo", empty]), 2)

    def test_tool_is_listed(self):
        import agent_tools
        self.assertIn("solsarch_rank", [t["name"] for t in agent_tools.TOOLS])


if __name__ == "__main__":
    unittest.main()
