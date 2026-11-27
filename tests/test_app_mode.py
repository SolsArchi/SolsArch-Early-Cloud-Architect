"""The app-style mode: staged phases, intake, two separate readiness statements and consistency checks."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from helpers import SCRIPTS, write
import app_mode

SCRIPT = SCRIPTS / "solsarch.py"


def make(repo):
    write(repo, "requirements.txt", "fastapi==0.110.0\npsycopg2-binary\nredis\n")
    write(repo, "app/main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write(repo, "Dockerfile", "FROM python:3.12-slim\nEXPOSE 8000\n")


class Stages(unittest.TestCase):
    def test_phase1_offloads_cost_and_infrastructure_but_lists_them(self):
        runs = {s["agent"]: s["runs"] for s in app_mode.stages("phase1")}
        for agent in ("7", "9", "10", "11", "5.5", "9.5"):
            self.assertFalse(runs[agent], agent)
        for agent in ("0.5", "1", "6", "8", "11.5", "12", "13"):
            self.assertTrue(runs[agent], agent)
        self.assertTrue(all(s["runs"] for s in app_mode.stages("full")))


class AppRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls.tmp.name)
        make(cls.repo)
        cls.env = dict(os.environ, SOLSARCH_OFFLINE="1", SOLSARCH_SKIP_VALIDATE="1", SOLSARCH_TODAY="2026-10-23", SOLSARCH_NO_HISTORY="1")
        cls.first = cls.cli("app", "run")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def cli(cls, *args):
        done = subprocess.run([sys.executable, "-I", str(SCRIPT)] + list(args) + ["--repo", str(cls.repo)], capture_output=True, text=True, env=cls.env)
        return done.returncode, json.loads(done.stdout)

    def result(self):
        return json.loads((self.repo / ".solsarch" / "app.json").read_text(encoding="utf-8"))

    def test_phase1_recommends_one_cloud_with_a_runner_up(self):
        code, out = self.first
        self.assertEqual(code, 0, out)
        self.assertIn(out["recommendation"]["recommended"], app_mode.APP_CLOUDS)
        self.assertNotEqual(out["recommendation"]["recommended"], out["recommendation"]["runner_up"])

    def test_phase1_delivers_no_cost_file_and_no_infrastructure(self):
        names = zipfile.ZipFile(self.repo / "docs" / "solsarch-phase1.zip").namelist()
        self.assertIn("solsarch-readiness.md", names)
        self.assertFalse([n for n in names if "cost" in n or n.endswith(".tf") or "iac" in n], names)
        self.assertFalse((self.repo / ".solsarch" / "iac").exists())
        self.assertFalse((self.repo / "docs" / "solsarch-costs.md").exists())

    def test_readiness_is_not_verified_and_never_ready_in_phase1(self):
        data = self.result()
        self.assertEqual(data["deployment_readiness"]["status"], "not_verified")
        self.assertLess(data["analysis_completeness"]["score"], 100)
        self.assertEqual(sum(p["of"] for p in data["analysis_completeness"]["parts"]), 100)

    def test_unchecked_items_are_never_pass(self):
        by = {i["id"]: i["status"] for i in self.result()["checklist"]}
        for key in ("dependencies", "cors", "auth", "debug", "http", "history"):
            self.assertEqual(by[key], "NOT_CHECKED", key)

    def test_no_invariant_is_violated_on_a_clean_repo(self):
        self.assertEqual(self.result()["invariants"]["violations"], [])

    def test_answering_intake_raises_completeness_and_closes_questions(self):
        before = self.result()["analysis_completeness"]["score"]
        code, view = self.cli("app", "intake", "--set", "business_driver=modernization", "--set", "audience=engineering")
        self.assertEqual(code, 0)
        self.assertNotIn("business_driver", [q["key"] for q in view["open"]])
        self.cli("app", "check")
        self.assertGreater(self.result()["analysis_completeness"]["score"], before)

    def test_a_bad_answer_is_rejected(self):
        self.assertEqual(self.cli("app", "intake", "--set", "cloud=mars")[0], 2)
        self.assertEqual(self.cli("app", "intake", "--set", "nonsense=1")[0], 2)

    def test_a_choice_the_plan_ignores_is_a_violation(self):
        data = self.result()
        app_mode.record_answers(self.repo, ["cloud=gcp"])
        try:
            if data["intake"] and json.loads((self.repo / ".solsarch" / "plan.json").read_text())["cloud"] != "gcp":
                ids = [v["id"] for v in app_mode.check(self.repo, "phase1")["invariants"]["violations"]]
                self.assertIn("I16", ids)
        finally:
            path = self.repo / ".solsarch" / "intake.json"
            saved = json.loads(path.read_text())
            saved["answers"].pop("cloud", None)
            path.write_text(json.dumps(saved))

    def test_price_text_in_a_phase1_report_fails(self):
        report = self.repo / "docs" / "solsarch-report.md"
        original = report.read_text(encoding="utf-8")
        try:
            report.write_text(original + "It costs $40 a month.\n", encoding="utf-8")
            ids = [v["id"] for v in app_mode.check(self.repo, "phase1")["invariants"]["violations"]]
            self.assertIn("I7", ids)
        finally:
            report.write_text(original, encoding="utf-8")

    def test_tool_is_listed(self):
        import agent_tools
        self.assertIn("solsarch_app", [t["name"] for t in agent_tools.TOOLS])


class Blocked(unittest.TestCase):
    def test_a_hardcoded_credential_blocks(self):
        from helpers import fake_aws_key
        with tempfile.TemporaryDirectory() as d:
            make(d)
            write(d, "app/keys.py", 'AWS_ACCESS_KEY_ID = "%s"\n' % fake_aws_key())
            env = dict(os.environ, SOLSARCH_OFFLINE="1", SOLSARCH_SKIP_VALIDATE="1", SOLSARCH_NO_HISTORY="1")
            subprocess.run([sys.executable, "-I", str(SCRIPT), "app", "run", "--repo", d, "--cloud", "aws"], capture_output=True, text=True, env=env)
            data = json.loads((Path(d) / ".solsarch" / "app.json").read_text(encoding="utf-8"))
            self.assertEqual(data["deployment_readiness"]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
