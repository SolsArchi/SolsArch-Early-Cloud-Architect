"""The evidence ledger labels every claim honestly and is a pure function of the saved files."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import SCRIPTS, write
import ledger

SCRIPT = SCRIPTS / "solsarch.py"


class Ledger(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls.tmp.name)
        write(cls.repo, "requirements.txt", "fastapi==0.110.0\npsycopg2-binary\nredis\n")
        write(cls.repo, "app/main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
        write(cls.repo, "Dockerfile", "FROM python:3.12-slim\nEXPOSE 8000\n")
        cls.env = dict(os.environ, SOLSARCH_OFFLINE="1", SOLSARCH_SKIP_VALIDATE="1", SOLSARCH_TODAY="2026-10-23", SOLSARCH_NO_HISTORY="1")
        for args in (["scan"], ["plan", "--cloud", "aws", "--compute", "server"], ["finish"]):
            cls.cli(*args)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def cli(cls, *args):
        done = subprocess.run([sys.executable, "-I", str(SCRIPT)] + list(args) + ["--repo", str(cls.repo)], capture_output=True, text=True, env=cls.env)
        return done.returncode, done.stdout

    def load(self):
        return json.loads((self.repo / ".solsarch" / "ledger.json").read_text(encoding="utf-8"))

    def test_finish_writes_the_ledger_and_the_zip_carries_it(self):
        self.assertTrue((self.repo / "docs" / "solsarch-evidence.md").exists())
        import zipfile
        names = zipfile.ZipFile(self.repo / "docs" / "solsarch-output.zip").namelist()
        self.assertTrue(any(n.endswith("solsarch-evidence.md") for n in names), names)

    def test_every_record_has_a_known_status_and_a_source(self):
        data = self.load()
        self.assertTrue(data["records"])
        for r in data["records"]:
            self.assertIn(r["status"], ledger.STATUSES)
            self.assertTrue(r["source"], r)
        self.assertEqual(sum(data["counts"].values()), len(data["records"]))

    def test_decisions_with_file_evidence_are_observed(self):
        decisions = [r for r in self.load()["records"] if r["kind"] == "decision"]
        self.assertTrue(decisions)
        self.assertTrue(all(r["status"] == "Observed" and ":" in r["source"] for r in decisions))

    def test_offline_nothing_is_called_verified_that_has_no_source(self):
        for r in self.load()["records"]:
            if r["status"] == "Verified":
                self.assertTrue(r["source"].startswith("http"), r)
        unpriced = [r for r in self.load()["records"] if r["kind"] == "price"]
        self.assertTrue(all(r["status"] == "Unknown" for r in unpriced))

    def test_ladder_reflects_what_ran(self):
        self.assertEqual(self.load()["ladder"]["level"], "Statically validated")
        self.assertEqual(ledger.ladder(self.repo / "nothing")["level"], "None")

    def test_same_input_gives_identical_bytes(self):
        first = (self.repo / ".solsarch" / "ledger.json").read_bytes()
        self.cli("ledger")
        self.assertEqual(first, (self.repo / ".solsarch" / "ledger.json").read_bytes())

    def test_a_live_price_is_verified_and_a_book_price_is_estimated(self):
        costs = {"lines": [{"label": "A", "monthly": 1, "quantity": 1, "unit": "h", "unit_price": 1, "origin": "live", "source": "https://aws.amazon.com/x", "resource_id": "r1"},
                           {"label": "B", "monthly": 2, "quantity": 1, "unit": "h", "unit_price": 2, "origin": "book", "source": "price book", "resource_id": "r2"}], "unpriced": []}
        statuses = {r["id"]: r["status"] for r in ledger.cost_records(costs)}
        self.assertEqual(statuses, {"price.r1": "Verified", "price.r2": "Estimated"})

    def test_the_audit_flags_a_verified_claim_with_no_proof_and_ignores_not_verified(self):
        data = {"counts": {"Verified": 0}}
        with tempfile.TemporaryDirectory() as d:
            write(Path(d), "docs/solsarch-report.md", "Prices are not verified." + chr(10) + "Unverified names." + chr(10) + "All prices are verified today." + chr(10))
            found = ledger.audit(d, data)
        self.assertEqual([c["line"] for c in found], [3])
        with tempfile.TemporaryDirectory() as d:
            write(Path(d), "docs/solsarch-report.md", "All prices are verified today." + chr(10))
            self.assertEqual(ledger.audit(d, {"counts": {"Verified": 2}}), [])

    def test_generated_documents_make_no_unsupported_verified_claim(self):
        self.assertEqual(self.load()["unsupported_claims"], [])

    def test_no_plan_is_an_error(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertIsNone(ledger.build(empty))
            self.assertEqual(ledger.main(["--repo", empty]), 2)

    def test_tool_is_listed(self):
        import agent_tools
        self.assertIn("solsarch_ledger", [t["name"] for t in agent_tools.TOOLS])


if __name__ == "__main__":
    unittest.main()
