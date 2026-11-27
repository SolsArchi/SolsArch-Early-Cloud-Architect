"""Approvals tie a named person to one plan and one rule set; a change voids them and the gate says so."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import SCRIPTS, write
import approvals

SCRIPT = SCRIPTS / "solsarch.py"


class Approvals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        write(self.repo, "requirements.txt", "fastapi==0.110.0\npsycopg2-binary\n")
        write(self.repo, "app/main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
        write(self.repo, "Dockerfile", "FROM python:3.12-slim\nEXPOSE 8000\n")
        self.env = dict(os.environ, SOLSARCH_OFFLINE="1", SOLSARCH_SKIP_VALIDATE="1", SOLSARCH_TODAY="2026-10-23", SOLSARCH_NO_HISTORY="1")
        for args in (["scan"], ["plan", "--cloud", "aws", "--compute", "server"], ["finish"]):
            self.cli(*args)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        done = subprocess.run([sys.executable, "-I", str(SCRIPT)] + list(args) + ["--repo", str(self.repo)], capture_output=True, text=True, env=self.env)
        return done.returncode, done.stdout

    def gate_checks(self):
        code, out = self.cli("gate")
        return {c["check"]: c for c in json.loads(out)["checks"]}

    def test_no_approval_is_skipped_unless_required(self):
        self.assertEqual(self.gate_checks()["Human approval"]["status"], "skipped")
        write(self.repo, "solsarch.gate.json", json.dumps({"require_approval": True}))
        self.assertEqual(self.gate_checks()["Human approval"]["status"], "fail")

    def test_approving_records_who_and_passes_the_gate(self):
        code, out = self.cli("approve", "--by", "Asha Rao", "--note", "OK for launch")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.gate_checks()["Human approval"]["status"], "pass")
        text = (self.repo / "docs" / "solsarch-approvals.md").read_text(encoding="utf-8")
        self.assertIn("Asha Rao", text)
        self.assertIn("2026-10-23", text)

    def test_a_plan_change_voids_the_approval(self):
        self.cli("approve", "--by", "Asha Rao")
        self.cli("plan", "--cloud", "azure", "--compute", "server")
        check = self.gate_checks()["Human approval"]
        self.assertEqual(check["status"], "fail")
        self.assertIn("plan changed", check["detail"])
        self.assertEqual(self.cli("approve", "status")[0], 1)

    def test_a_rules_change_voids_the_approval(self):
        self.cli("approve", "--by", "Asha Rao")
        write(self.repo, ".solsarch/rules/company.yaml", "rules:\n  - kind: allowed_clouds\n    value: [aws, azure]\n    level: required\n")
        self.cli("rules", "ingest")
        self.assertEqual(approvals.status(self.repo)["state"], "stale")

    def test_name_is_required_and_text_is_trimmed(self):
        self.assertEqual(self.cli("approve")[0], 2)
        self.assertEqual(approvals.clean("  a\n\n b  ", 10), "a b")
        self.assertEqual(len(approvals.clean("x" * 500, approvals.NOTE_LIMIT)), approvals.NOTE_LIMIT)

    def test_history_is_kept_and_listed_newest_first(self):
        self.cli("approve", "--by", "First")
        self.cli("approve", "--by", "Second")
        text = (self.repo / "docs" / "solsarch-approvals.md").read_text(encoding="utf-8")
        self.assertLess(text.index("Second"), text.index("First"))
        self.assertEqual(len(approvals.load(self.repo)), 2)

    def test_tool_is_listed(self):
        import agent_tools
        self.assertIn("solsarch_approve", [t["name"] for t in agent_tools.TOOLS])


if __name__ == "__main__":
    unittest.main()
