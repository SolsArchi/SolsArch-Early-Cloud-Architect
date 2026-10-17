import json
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

import helpers
from helpers import write

import build_report
import diagram_sanitizer

GOOD_URL = "https://docs.aws.amazon.com/AmazonECS/latest/developerguide/AWS_Fargate.html"
NL = chr(10)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "demo-app"
        self.repo.mkdir()
        helpers.build_demo(self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def report(self, **kwargs):
        helpers.pipeline(self.repo, **kwargs)
        return (self.repo / "docs" / "solsarch-report.md").read_text(encoding="utf-8")

    def test_sections_in_order(self):
        text = self.report()
        headings = re.findall(r"^## (\d+)\. (.+)$", text, re.M)
        self.assertEqual([int(n) for n, _ in headings], list(range(1, 12)))
        self.assertEqual([h for _, h in headings][:3], ["Verdict", "Summary", "Your choices"])
        self.assertIn("Why each service was chosen", [h for _, h in headings])
        self.assertIn("Diagrams", [h for _, h in headings])

    def test_every_decision_is_explained(self):
        text = self.report()
        plan = json.loads((self.repo / ".solsarch" / "plan.json").read_text(encoding="utf-8"))
        section = text.split(NL + "## 5. ")[1].split(NL + "## 6. ")[0]
        self.assertEqual(len(re.findall(r"^### \d+\. ", section, re.M)), len(plan["decisions"]))
        for decision in plan["decisions"]:
            self.assertIn(decision["service"], section)
            self.assertIn(decision["why"], section)
        for label in ("Why this service", "Evidence from your code", "Web check", "Decision record"):
            self.assertGreaterEqual(section.count("**%s:**" % label), len(plan["decisions"]))

    def test_every_diagram_is_embedded_and_explained(self):
        text = self.report()
        section = text.split(NL + "## 6. ")[1].split(NL + "## 7. ")[0]
        blocks = re.findall(r"```mermaid\n(.*?)\n```", section, re.S)
        self.assertGreaterEqual(len(blocks), 5)
        for block in blocks:
            self.assertEqual(diagram_sanitizer.sanitize(block)[2], [])
        self.assertEqual(section.count("**What it shows.**"), len(blocks))
        self.assertEqual(section.count("**How to read it.**"), len(blocks))
        self.assertGreaterEqual(section.count("**What to notice.**"), len(blocks) - 1)
        self.assertEqual(section.count("*Limits:"), len(blocks))

    def test_verdict_is_computed(self):
        text = self.report()
        self.assertIn("**Not ready for production. 2 blockers to fix first: SEC-003, SEC-005.**", text)
        clean = Path(self._tmp.name) / "clean"
        clean.mkdir()
        write(clean, "package.json", '{"dependencies": {"next": "14"}}')
        helpers.pipeline(clean)
        self.assertIn("No blockers found in the scanned files.", (clean / "docs" / "solsarch-report.md").read_text(encoding="utf-8"))

    def test_never_leaks_secret_values(self):
        helpers.pipeline(self.repo)
        for path in list(self.repo.rglob("*.md")) + list((self.repo / ".solsarch").glob("*.json")):
            self.assertNotIn(helpers.fake_aws_key(), path.read_text(encoding="utf-8"), path.name)
        with zipfile.ZipFile(self.repo / "docs" / "solsarch-output.zip") as archive:
            for name in archive.namelist():
                self.assertNotIn(helpers.fake_aws_key().encode(), archive.read(name), name)

    def test_assumptions_are_marked_until_confirmed(self):
        self.assertIn("assumed, not confirmed", self.report(assumed=True))
        self.assertNotIn("assumed, not confirmed", self.report(assumed=False))

    def test_web_check_states(self):
        helpers.cli("scan", "--repo", self.repo)
        helpers.cli("plan", "--repo", self.repo, "--cloud", "aws", "--compute", "server")
        helpers.cli("facts", "add", "--repo", self.repo, "--key", "compute_api", "--url", GOOD_URL,
                    "--quote", "AWS Fargate is a technology that you can use with Amazon ECS", "--verdict", "confirmed")
        helpers.cli("facts", "add", "--repo", self.repo, "--key", "database",
                    "--url", "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/CHAP_AuroraOverview.html",
                    "--quote", "Amazon Aurora is a fully managed relational database engine", "--verdict", "changed",
                    "--note", "Now documented under a new heading")
        code, out = helpers.cli("finish", "--repo", self.repo)
        self.assertEqual(code, 0, out)
        text = (self.repo / "docs" / "solsarch-report.md").read_text(encoding="utf-8")
        self.assertIn("Verified on", text)
        self.assertIn("Changed, review", text)
        self.assertIn("Not verified", text)
        self.assertIn("Now documented under a new heading", text)
        self.assertIn("the web check says changed", text)
        self.assertIn(GOOD_URL, text)

    def test_skipped_web_check_is_stated(self):
        text = self.report()
        self.assertIn("The web check was skipped: test run", text)
        self.assertIn("web search skipped", text)

    def test_narrative_is_included(self):
        helpers.cli("scan", "--repo", self.repo)
        helpers.cli("plan", "--repo", self.repo, "--cloud", "gcp", "--compute", "serverless")
        note = self.repo / "notes.md"
        note.write_text("Please check the key in config.py first.", encoding="utf-8")
        code, _ = helpers.cli("finish", "--repo", self.repo, "--narrative", note)
        self.assertEqual(code, 0)
        text = (self.repo / "docs" / "solsarch-report.md").read_text(encoding="utf-8")
        self.assertIn("### Notes\n\nPlease check the key in config.py first.", text)

    def test_needs_scan_and_plan(self):
        code, out = helpers.cli("finish", "--repo", self.repo)
        self.assertEqual((code, out["status"]), (2, "error"))
        self.assertIn("Run the scan and plan steps", json.dumps(out))

    def test_table_cell_escaping(self):
        self.assertEqual(build_report.cell("a|b\nc"), "a\\|b c")
        self.assertEqual(build_report.cell(None), "-")
        self.assertEqual(build_report.cell(0), "0")

    def test_diagram_files_are_replaced_on_rerun(self):
        helpers.pipeline(self.repo, "aws", "server")
        (self.repo / "docs" / "diagrams" / "99-stale.mmd").write_text("flowchart TD\n", encoding="utf-8")
        helpers.pipeline(self.repo, "azure", "serverless")
        names = sorted(p.name for p in (self.repo / "docs" / "diagrams").glob("*.mmd"))
        self.assertNotIn("99-stale.mmd", names)
        self.assertTrue(names)


if __name__ == "__main__":
    unittest.main()
