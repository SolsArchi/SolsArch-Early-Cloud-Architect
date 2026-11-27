"""The workload spec states needs, with evidence, before any provider is chosen."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import SCRIPTS, write
import workload

SCRIPT = SCRIPTS / "solsarch.py"


class Workload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls.tmp.name)
        write(cls.repo, "requirements.txt", "fastapi==0.110.0\npsycopg2-binary\nredis\n")
        write(cls.repo, "app/main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
        write(cls.repo, "Dockerfile", "FROM python:3.12-slim\nEXPOSE 8000\n")
        env = dict(os.environ, SOLSARCH_OFFLINE="1", SOLSARCH_SKIP_VALIDATE="1", SOLSARCH_TODAY="2026-10-23", SOLSARCH_NO_HISTORY="1")
        for args in (["scan"], ["plan", "--cloud", "aws", "--compute", "server"], ["finish"]):
            subprocess.run([sys.executable, "-I", str(SCRIPT)] + args + ["--repo", str(cls.repo)], capture_output=True, text=True, env=env)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_finish_writes_it_and_it_names_no_provider_service(self):
        data = json.loads((self.repo / ".solsarch" / "workload.json").read_text(encoding="utf-8"))
        self.assertEqual(data["compute"][0]["frameworks"], ["FastAPI"])
        self.assertEqual(data["compute"][0]["port"], 8000)
        self.assertTrue(data["compute"][0]["evidence"])
        text = json.dumps(data)
        for service in ("Fargate", "ElastiCache", "Aurora"):
            self.assertNotIn(service, text)
        self.assertTrue((self.repo / "docs" / "solsarch-workload.md").exists())

    def test_data_stores_come_from_the_scan(self):
        data = json.loads((self.repo / ".solsarch" / "workload.json").read_text(encoding="utf-8"))
        self.assertTrue(data["data_stores"])
        self.assertTrue(all(s["evidence"] for s in data["data_stores"]))

    def test_no_plan_gives_nothing(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertIsNone(workload.build(empty))
            self.assertEqual(workload.main(["--repo", empty]), 2)

    def test_tool_is_listed(self):
        import agent_tools
        self.assertIn("solsarch_workload", [t["name"] for t in agent_tools.TOOLS])


if __name__ == "__main__":
    unittest.main()
