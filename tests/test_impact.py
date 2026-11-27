"""Impact analysis shows what a code change does to the architecture, and nothing when nothing changed."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import SCRIPTS, write
import impact

SCRIPT = SCRIPTS / "solsarch.py"
STREAM = ("from fastapi.responses import StreamingResponse\nfrom app.main import app\nimport openai\n\n@app.get('/chat')\ndef chat():\n"
          "    def gen():\n        for c in openai.chat.completions.create(model='x', messages=[], stream=True):\n            yield 'data: %s\n\n' % c\n"
          "    return StreamingResponse(gen(), media_type='text/event-stream')\n")


class Impact(unittest.TestCase):
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

    def test_no_baseline_is_a_clear_error(self):
        code, out = self.cli("impact", "check")
        self.assertEqual(code, 2)
        self.assertIn("No baseline", out)

    def test_unchanged_code_gives_an_empty_impact(self):
        self.assertEqual(self.cli("impact", "baseline")[0], 0)
        code, out = self.cli("impact", "check", "--fail-on-change")
        self.assertEqual(code, 0, out)
        data = json.loads((self.repo / ".solsarch" / "impact.json").read_text(encoding="utf-8"))
        self.assertFalse(data["changed"])
        self.assertIn("no architecture change", (self.repo / ".solsarch" / "impact-comment.md").read_text(encoding="utf-8"))

    def test_a_streaming_endpoint_is_reported_with_its_file_and_line(self):
        self.cli("impact", "baseline")
        write(self.repo, "app/stream.py", STREAM)
        code, out = self.cli("impact", "check", "--fail-on-change")
        self.assertEqual(code, 1, out)
        data = json.loads((self.repo / ".solsarch" / "impact.json").read_text(encoding="utf-8"))
        self.assertEqual([s["signal"] for s in data["signals"]], ["streaming"])
        self.assertTrue(all(e.startswith("app/stream.py:") for e in data["signals"][0]["evidence"]))
        self.assertIn("120 seconds", data["signals"][0]["meaning"])
        text = (self.repo / "docs" / "solsarch-impact.md").read_text(encoding="utf-8")
        self.assertIn("Workload: streaming turned on", text)

    def test_a_new_dependency_adds_a_service_and_removal_reverses_it(self):
        self.cli("impact", "baseline")
        write(self.repo, "requirements.txt", "fastapi==0.110.0\npsycopg2-binary\nredis\n")
        self.cli("impact", "check")
        data = json.loads((self.repo / ".solsarch" / "impact.json").read_text(encoding="utf-8"))
        self.assertTrue(any("Redis" in s["service"] or "ElastiCache" in s["service"] for s in data["services_added"]), data["services_added"])
        self.assertFalse(data["services_removed"])

    def test_output_is_identical_on_a_second_run(self):
        self.cli("impact", "baseline")
        write(self.repo, "app/stream.py", STREAM)
        self.cli("impact", "check")
        first = (self.repo / ".solsarch" / "impact.json").read_bytes()
        self.cli("impact", "check")
        self.assertEqual(first, (self.repo / ".solsarch" / "impact.json").read_bytes())

    def test_baseline_without_a_plan_is_an_error(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(impact.save_baseline(empty)["status"], "error")

    def test_the_check_does_not_overwrite_the_saved_plan(self):
        self.cli("impact", "baseline")
        before = (self.repo / ".solsarch" / "plan.json").read_bytes()
        write(self.repo, "app/stream.py", STREAM)
        self.cli("impact", "check")
        self.assertEqual(before, (self.repo / ".solsarch" / "plan.json").read_bytes())

    def test_tool_is_listed(self):
        import agent_tools
        self.assertIn("solsarch_impact", [t["name"] for t in agent_tools.TOOLS])


if __name__ == "__main__":
    unittest.main()
