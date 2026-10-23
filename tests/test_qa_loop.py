"""The quality stage repairs what a script can repair, stops when nothing changes, and logs every change."""
import json
import os
import subprocess
import sys
import unittest

import helpers  # noqa: F401
import qa
import qa_gates
from test_eval_score import FreeTierServerlessScore, SCRIPT


class QualityLoop(FreeTierServerlessScore):
    def step(self, *args):
        done = subprocess.run([sys.executable, "-I", str(SCRIPT)] + list(args) + ["--repo", str(self.repo)], capture_output=True, text=True, env=self.env)
        return done.stdout

    def fixers(self):
        return {"costs": lambda: self.step("costs", "--offline"), "iac": lambda: self.step("iac")}

    def setUp(self):
        # restore exactly what was there: other tests rely on the offline default set in conftest.py
        self.saved = {k: os.environ.get(k) for k in ("SOLSARCH_TODAY", "SOLSARCH_OFFLINE")}
        os.environ["SOLSARCH_TODAY"] = "2026-10-23"
        os.environ["SOLSARCH_OFFLINE"] = "1"
        self.step("costs", "--offline")
        self.step("iac")
        self.step("contract")

    def tearDown(self):
        for key, old in self.saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    def test_nothing_to_fix_means_no_changes(self):
        result = qa.run(self.repo, self.fixers())
        self.assertEqual(result["qa_log"], [])

    def test_missing_cost_report_is_rebuilt_and_logged(self):
        (self.repo / ".solsarch" / "costs.json").unlink()
        broken = qa_gates.evaluate(self.repo)
        self.assertTrue(broken["not_verified"])
        result = qa.run(self.repo, self.fixers())
        self.assertEqual([e["step"] for e in result["qa_log"]], ["costs"])
        self.assertEqual(result["qa_log"][0]["pass"], 1)
        self.assertGreaterEqual(result["score"], broken["score"])
        c1 = next(c for g in result["gates"] for c in g["checks"] if c["id"] == "C1")
        self.assertEqual(c1["status"], "PASS")

    def test_a_contradiction_is_found_and_the_code_regenerated(self):
        path = self.repo / ".solsarch" / "iac-manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["resources"].append({"type": "aws_nat_gateway", "name": "x", "file": "terraform/main.tf"})
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.step("contract")
        broken = qa_gates.evaluate(self.repo)
        self.assertTrue(broken["blockers"])
        self.assertTrue(broken["contradictions"])
        result = qa.run(self.repo, self.fixers())
        self.assertIn("iac", [e["step"] for e in result["qa_log"]])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["contradictions"], [])

    def test_a_repair_is_tried_once_only(self):
        calls = []
        (self.repo / ".solsarch" / "costs.json").unlink()
        result = qa.run(self.repo, {"costs": lambda: calls.append(1) or "did nothing"})
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(len(result["qa_log"]), qa.MAX_PASSES)

    def test_report_is_written_with_the_facts_it_relies_on(self):
        result = qa.run(self.repo, self.fixers())
        qa.write(self.repo, result)
        text = (self.repo / "docs" / "solsarch-quality.md").read_text(encoding="utf-8")
        for needle in ("# Architecture quality report", "## The seven gates", "## Facts this plan relies on", "docs.aws.amazon.com", "Limits of this report"):
            self.assertIn(needle, text)
        contract = json.loads((self.repo / ".solsarch" / "contract.json").read_text(encoding="utf-8"))
        self.assertEqual(contract["evaluation"]["score"], result["score"])


if __name__ == "__main__":
    unittest.main()
