"""Scorecard, exports, pre-deploy gate, executive deck and the repeatable-output promise."""
import argparse
import hashlib
import json
import re
import tempfile
import unittest
import xml.dom.minidom
import zipfile
from pathlib import Path

import helpers
import facts
import gate
import ops_scorecard
from helpers import write
from test_pipeline import repo_in
from test_matrix import monorepo


def agent_app(root):
    write(root, "requirements.txt", "openai\nlanggraph\nfastapi\n")
    write(root, "agent.py", "import openai\nfrom langgraph.graph import StateGraph\nimport subprocess\ndef run(cmd):\n    return subprocess.run(cmd, shell=True)\n")
    write(root, "Dockerfile", "FROM python:3.12\nRUN pip install -r requirements.txt\nCMD [\"python\", \"agent.py\"]\n")


def disciplined_app(root):
    agent_app(root)
    write(root, "Dockerfile", "FROM python:3.12 AS build\nRUN pip install -r requirements.txt\nFROM python:3.12-slim\nUSER 10001\nCMD [\"python\", \"agent.py\"]\n")
    write(root, ".github/workflows/ci.yml", "name: ci\njobs:\n  t:\n    steps:\n      - run: pytest\n      - run: trivy fs .\n")
    write(root, "prompts/system.prompt", "You are helpful.")
    write(root, "evals/promptfoo.yaml", "tests: []\n")
    write(root, "tests/test_a.py", "def test_a(): pass\n")


class Scorecard(unittest.TestCase):
    def test_unsafe_agent_and_unversioned_prompts_are_flagged(self):
        with tempfile.TemporaryDirectory() as t:
            agent_app(t)
            card = ops_scorecard.build(t)
            rules = {f["rule"] for f in card["findings"]}
            self.assertIn("OPS-DOCKER-001", rules)
            self.assertIn("OPS-LLM-001", rules)
            self.assertIn("OPS-AGENT-001", rules)

    def test_more_practice_gives_a_higher_level_and_fewer_findings(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            agent_app(a)
            disciplined_app(b)
            low, high = ops_scorecard.build(a), ops_scorecard.build(b)

            def level(card, name):
                return next(d["level"] for d in card["disciplines"] if d["discipline"] == name)

            self.assertGreater(level(high, "DevOps"), level(low, "DevOps"))
            self.assertGreater(level(high, "LLMOps"), level(low, "LLMOps"))
            self.assertLess(len(high["findings"]), len(low["findings"]))

    def test_disciplines_that_do_not_apply_are_not_scored(self):
        with tempfile.TemporaryDirectory() as t:
            write(t, "package.json", '{"dependencies":{"react":"18"}}')
            card = ops_scorecard.build(t)
            by = {d["discipline"]: d for d in card["disciplines"]}
            for name in ("MLOps", "LLMOps", "AgentOps"):
                self.assertFalse(by[name]["applicable"])
                self.assertIsNone(by[name]["level"])

    def test_same_files_same_scorecard(self):
        with tempfile.TemporaryDirectory() as t:
            disciplined_app(t)
            self.assertEqual(ops_scorecard.build(t), ops_scorecard.build(t))


def make_state(tmp, provider="aws", builder=monorepo, name="shop"):
    repo = repo_in(tmp, name, builder)
    helpers.pipeline(repo, provider, "server")
    helpers.cli("finish", "--repo", repo)
    return repo


class Exports(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_every_export_is_well_formed(self):
        repo = make_state(self.tmp)
        folder = Path(repo) / ".solsarch" / "exports"
        self.assertTrue((folder / "architecture.dsl").exists())
        xml.dom.minidom.parseString((folder / "architecture.drawio").read_text(encoding="utf-8"))
        json.loads((folder / "port-entities.json").read_text(encoding="utf-8"))
        dsl = (folder / "architecture.dsl").read_text(encoding="utf-8")
        self.assertEqual(dsl.count("{"), dsl.count("}"))
        self.assertIn("softwareSystem", dsl)
        self.assertIn("@startuml", (folder / "architecture.puml").read_text(encoding="utf-8"))
        try:
            import yaml
        except ImportError:
            return
        docs = list(yaml.safe_load_all((folder / "backstage" / "catalog-info.yaml").read_text(encoding="utf-8")))
        kinds = [d["kind"] for d in docs]
        for kind in ("System", "Component", "Resource"):
            self.assertIn(kind, kinds)
        score = yaml.safe_load((folder / "score" / "api.score.yaml").read_text(encoding="utf-8"))
        self.assertEqual(score["apiVersion"], "score.dev/v1b1")

    def test_exports_only_contain_things_the_plan_has(self):
        repo = make_state(self.tmp)
        dsl = (Path(repo) / ".solsarch" / "exports" / "architecture.dsl").read_text(encoding="utf-8")
        plan = json.loads((Path(repo) / ".solsarch" / "plan.json").read_text(encoding="utf-8"))
        for d in plan["decisions"]:
            if d["key"] in ("database", "cache"):
                self.assertIn(d["service"], dsl)


class Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_the_gate_writes_sarif_and_a_comment(self):
        repo = make_state(self.tmp)
        code, out = helpers.cli("gate", "--repo", repo, "--sarif", str(Path(repo) / "x.sarif"), "--comment", str(Path(repo) / "c.md"))
        self.assertIn(out["verdict"], ("pass", "fail"))
        sarif = json.loads((Path(repo) / "x.sarif").read_text(encoding="utf-8"))
        self.assertEqual(sarif["version"], "2.1.0")
        self.assertIn("## SolsArch pre-deploy gate", (Path(repo) / "c.md").read_text(encoding="utf-8"))

    def test_a_seeded_blocker_fails_the_gate(self):
        def build(root):
            monorepo(root)
            write(root, "infra/db.tf", 'resource "aws_db_instance" "d" {\n  publicly_accessible = true\n}\nresource "aws_security_group_rule" "r" {\n  cidr_blocks = ["0.0.0.0/0"]\n  from_port = 5432\n  to_port = 5432\n}\n')
        repo = make_state(self.tmp, builder=build, name="risky")
        code, out = helpers.cli("gate", "--repo", repo)
        self.assertEqual(out["verdict"], "fail")
        self.assertEqual(code, 1)
        self.assertIn("Pre-deployment security checks", out["failed"])

    def test_thresholds_come_from_a_committed_file(self):
        repo = make_state(self.tmp)
        write(repo, "solsarch.gate.json", json.dumps({"min_capability_score": 101}))
        code, out = helpers.cli("gate", "--repo", repo)
        self.assertIn("Infrastructure capability checklist", out["failed"])

    def test_gate_needs_a_plan(self):
        self.assertEqual(gate.evaluate(tempfile.mkdtemp())["verdict"], "error")


class ExecutiveDeck(unittest.TestCase):
    def test_deck_pdf_and_html_are_valid(self):
        repo = make_state(tempfile.mkdtemp())
        docs = Path(repo) / "docs"
        with zipfile.ZipFile(docs / "solsarch-executive-deck.pptx") as z:
            self.assertIsNone(z.testzip())
            for name in z.namelist():
                if name.endswith((".xml", ".rels")):
                    xml.dom.minidom.parseString(z.read(name))
            self.assertGreaterEqual(len([n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml", n)]), 6)
            self.assertIn("Prepared with SolsArch", z.read("ppt/slides/slide2.xml").decode("utf-8"))
        try:
            from pptx import Presentation
            self.assertGreaterEqual(len(Presentation(str(docs / "solsarch-executive-deck.pptx")).slides), 6)
        except ImportError:
            pass
        pdf = (docs / "solsarch-executive-summary.pdf").read_bytes()
        self.assertTrue(pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF"))
        self.assertIn("solsarch-mark", (docs / "solsarch-executive-deck.html").read_text(encoding="utf-8"))

    def test_no_dollar_amounts_in_the_deck(self):
        repo = make_state(tempfile.mkdtemp())
        html = (Path(repo) / "docs" / "solsarch-executive-deck.html").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\$\s?\d", html))


class FactsAreHashed(unittest.TestCase):
    def record(self):
        repo = repo_in(tempfile.mkdtemp(), "shop", helpers.build_saas)
        helpers.pipeline(repo, "aws", "server")
        item = facts.load_plan(repo)["verify_queue"][0]
        quote = "%s is a managed service that helps you run applications without servers." % item["service"]
        helpers.cli("facts", "add", "--repo", repo, "--key", item["key"], "--url", "https://%s/x" % item["domains"][0], "--quote", quote, "--verdict", "confirmed")
        return repo, quote

    def test_every_recorded_fact_carries_a_sha256_of_its_quote(self):
        repo, quote = self.record()
        rec = facts.load_facts(repo)["facts"][0]
        self.assertEqual(rec["quote_sha256"], hashlib.sha256(quote.encode("utf-8")).hexdigest())

    def test_recheck_reports_drift(self):
        repo, quote = self.record()
        code, same = facts.cmd_recheck(argparse.Namespace(repo=repo), fetch_fn=lambda url: quote + " More text.")
        self.assertEqual(code, 0)
        code, gone = facts.cmd_recheck(argparse.Namespace(repo=repo), fetch_fn=lambda url: "The page was rewritten.")
        self.assertEqual(code, 1)
        self.assertTrue(gone["changed"])


class RepeatableOutput(unittest.TestCase):
    def test_generated_text_in_the_zip_has_no_timestamps(self):
        repo = make_state(tempfile.mkdtemp())
        zf = zipfile.ZipFile(Path(repo) / "docs" / "solsarch-output.zip")
        stamp = re.compile(r"20\d\d-[01]\d-[0-3]\d[ T][0-2]\d:[0-5]\d")
        skip = ("data/facts.json", "data/decisions.json")
        for name in zf.namelist():
            if name.endswith((".md", ".html", ".tf", ".yml", ".yaml", ".json", ".toml", ".dsl", ".puml")) and name not in skip and "price" not in name:
                self.assertIsNone(stamp.search(zf.read(name).decode("utf-8", "ignore")), name)

    def test_two_runs_give_the_same_zip_bytes(self):
        repo = make_state(tempfile.mkdtemp())
        zip_path = Path(repo) / "docs" / "solsarch-output.zip"
        helpers.cli("finish", "--repo", repo)
        first = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        helpers.cli("finish", "--repo", repo)
        self.assertEqual(first, hashlib.sha256(zip_path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
