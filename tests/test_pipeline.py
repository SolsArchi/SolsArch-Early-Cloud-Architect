import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import helpers
from helpers import write

EXPECTED_ZIP = {"README.txt", "solsarch-report.md", "solsarch-architecture.md", "data/plan.json", "data/scan.json",
                "data/facts.json", "data/decisions.json", "diagrams/index.html"}


def repo_in(tmp, name, builder):
    root = Path(tmp) / name
    root.mkdir()
    builder(root)
    return root


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_full_run_produces_everything_in_one_zip(self):
        repo = repo_in(self.tmp, "shop", helpers.build_demo)
        out = helpers.pipeline(repo, "azure", "serverless")
        self.assertEqual(out["status"], "done")
        self.assertGreaterEqual(out["diagram_count"], 5)
        self.assertEqual(Path(out["zip"]), repo / "docs" / "solsarch-output.zip")
        with zipfile.ZipFile(out["zip"]) as archive:
            self.assertIsNone(archive.testzip())
            names = set(archive.namelist())
            self.assertEqual(EXPECTED_ZIP - names, set())
            mmd = {n for n in names if n.endswith(".mmd")}
            self.assertEqual(len(mmd), out["diagram_count"])
            report = archive.read("solsarch-report.md").decode("utf-8")
            viewer = archive.read("diagrams/index.html").decode("utf-8")
        self.assertEqual(report, (repo / "docs" / "solsarch-report.md").read_text(encoding="utf-8"))
        self.assertIn("mermaid", viewer)
        self.assertEqual(viewer.count('class="mermaid"'), out["diagram_count"])
        self.assertIn("Azure Functions", report)

    def test_same_inputs_give_identical_outputs_in_different_folders(self):
        results = []
        for number in ("one", "two"):
            parent = Path(self.tmp) / number
            parent.mkdir()
            repo = repo_in(parent, "shop", helpers.build_demo)
            helpers.pipeline(repo, "gcp", "server")
            results.append(repo)
        first, second = results
        for relative in (".solsarch/plan.json", "docs/solsarch-architecture.md"):
            self.assertEqual((first / relative).read_bytes(), (second / relative).read_bytes(), relative)
        for path in sorted((first / "docs" / "diagrams").glob("*.mmd")):
            self.assertEqual(path.read_bytes(), (second / "docs" / "diagrams" / path.name).read_bytes())
        self.assertNotIn(bytes([13, 10]), (first / "docs" / "solsarch-report.md").read_bytes())  # LF only, on every OS

    def test_rerun_is_stable(self):
        repo = repo_in(self.tmp, "shop", helpers.build_demo)
        helpers.pipeline(repo)
        plan_one = (repo / ".solsarch" / "plan.json").read_bytes()
        adrs_one = len(json.loads((repo / ".solsarch" / "memory.json").read_text(encoding="utf-8"))["architectural_decisions"])
        helpers.pipeline(repo)
        self.assertEqual(plan_one, (repo / ".solsarch" / "plan.json").read_bytes())
        memory = json.loads((repo / ".solsarch" / "memory.json").read_text(encoding="utf-8"))
        self.assertEqual(len(memory["architectural_decisions"]), adrs_one)
        ids = [d["id"] for d in memory["architectural_decisions"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_changing_the_answer_replaces_decisions_without_duplicates(self):
        repo = repo_in(self.tmp, "shop", helpers.build_demo)
        helpers.pipeline(repo, "aws", "server")
        # approve one decision, then switch to serverless: the approved record must survive, a stale proposed one must not
        script = str(helpers.SCRIPTS / "memory_harness.py")
        subprocess.run([sys.executable, "-I", "-B", script, "--path", str(repo), "adr", "--id", "ADR-001",
                        "--status", "APPROVED"], capture_output=True, text=True, check=True)
        helpers.pipeline(repo, "aws", "serverless")
        records = json.loads((repo / ".solsarch" / "memory.json").read_text(encoding="utf-8"))["architectural_decisions"]
        keys = [r["key"] for r in records if r.get("status") != "SUPERSEDED"]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertNotIn("registry", keys)  # not used in serverless mode
        approved = [r for r in records if r["status"] == "APPROVED"]
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["id"], "ADR-001")

    def test_questions_ask_only_cloud_and_compute(self):
        repo = repo_in(self.tmp, "shop", helpers.build_demo)
        code, out = helpers.cli("questions", "--repo", repo)
        self.assertEqual([q["id"] for q in out["ask"]], ["cloud", "compute", "account_type"])
        values = [o["value"] for o in out["ask"][0]["options"]]
        self.assertEqual(values[:3], ["aws", "azure", "gcp"])
        self.assertTrue({"vercel", "hetzner", "supabase", "cloudflare"} <= set(values))
        self.assertFalse({"sap", "salesforce"} & set(values))
        self.assertEqual([o["value"] for o in out["ask"][1]["options"]], ["server", "serverless"])
        text = json.dumps(out).lower()
        for word in ("region", "budget", "compliance"):
            self.assertNotIn(word, json.dumps(out["ask"]).lower())
        self.assertIn("do not ask about region", text)

    def test_confirmed_answers_are_not_asked_again_but_assumed_ones_are(self):
        repo = repo_in(self.tmp, "shop", helpers.build_demo)
        helpers.pipeline(repo, "aws", "server", assumed=True)
        self.assertEqual(len(helpers.cli("questions", "--repo", repo)[1]["ask"]), 3)
        helpers.pipeline(repo, "aws", "server", assumed=False)
        out = helpers.cli("questions", "--repo", repo)[1]
        self.assertEqual([q["id"] for q in out["ask"]], ["account_type"])
        helpers.cli("profile", "--repo", repo, "--set", "account_type=standard")
        out = helpers.cli("questions", "--repo", repo)[1]
        self.assertEqual(out["ask"], [])
        self.assertEqual(out["already_known"], {"cloud": "aws", "compute": "server", "account_type": "standard"})

    def test_default_cloud_follows_the_code(self):
        def build(root):
            write(root, "requirements.txt", "flask\ngoogle-cloud-storage\ngoogle-cloud-pubsub\n")
        repo = repo_in(self.tmp, "gcpapp", build)
        helpers.cli("scan", "--repo", repo)
        self.assertEqual(helpers.cli("questions", "--repo", repo)[1]["if_user_cannot_answer"]["cloud"], "gcp")
        self.assertEqual(helpers.cli("scan", "--repo", repo)[1]["suggested_cloud"], "gcp")

    def test_memory_remembers_choices_for_the_next_session(self):
        repo = repo_in(self.tmp, "shop", helpers.build_demo)
        helpers.pipeline(repo, "azure", "server")
        done = subprocess.run([sys.executable, "-I", "-B", str(helpers.SCRIPTS / "memory_harness.py"),
                               "--path", str(repo), "read"], capture_output=True, text=True)
        message = json.loads(done.stdout)["message"]
        self.assertIn("azure, server", message)
        self.assertIn("recorded decisions", message)

    def test_nothing_to_draw_still_gives_a_report_and_zip(self):
        repo = repo_in(self.tmp, "empty", lambda r: write(r, "README.md", "# nothing here\n"))
        out = helpers.pipeline(repo)
        self.assertEqual(out["diagram_count"], 0)
        report = (repo / "docs" / "solsarch-report.md").read_text(encoding="utf-8")
        self.assertIn("No known components were found", report)
        self.assertIn("No diagrams were drawn", report)
        self.assertIn("No application framework", report)
        with zipfile.ZipFile(out["zip"]) as archive:
            self.assertIn("diagrams/index.html", archive.namelist())

    def test_terraform_only_repo(self):
        repo = repo_in(self.tmp, "infra", helpers.build_terraform)
        out = helpers.pipeline(repo)
        self.assertEqual(out["status"], "done")
        self.assertIn("SEC-001", (repo / "docs" / "solsarch-report.md").read_text(encoding="utf-8"))
        self.assertGreaterEqual(out["blockers"], 3)

    def test_plan_requires_scan_and_valid_choice(self):
        repo = repo_in(self.tmp, "shop", helpers.build_demo)
        code, out = helpers.cli("plan", "--repo", repo, "--cloud", "aws", "--compute", "server")
        self.assertEqual((code, out["status"]), (2, "error"))
        code, out = helpers.cli("scan", "--repo", repo / "missing")
        self.assertEqual(code, 2)

    def test_zip_is_reproducible(self):
        repo = repo_in(self.tmp, "shop", helpers.build_demo)
        helpers.pipeline(repo)
        first = (repo / "docs" / "solsarch-output.zip").read_bytes()
        helpers.cli("finish", "--repo", repo)
        # the report carries a timestamp, so only the member list and sizes of fixed files are compared
        with zipfile.ZipFile(repo / "docs" / "solsarch-output.zip") as again, \
                zipfile.ZipFile(io.BytesIO(first)) as before:
            self.assertEqual(again.namelist(), before.namelist())
            for name in ("data/plan.json", "solsarch-architecture.md", "diagrams/index.html"):
                self.assertEqual(again.read(name), before.read(name), name)

    def test_scan_and_state_do_not_leak_local_paths(self):
        repo = repo_in(self.tmp, "shop", helpers.build_demo)
        helpers.pipeline(repo)
        for path in (repo / ".solsarch").glob("*.json"):
            self.assertNotIn(self.tmp.replace("\\", "/"), path.read_text(encoding="utf-8").replace("\\\\", "/"), path.name)


if __name__ == "__main__":
    unittest.main()
