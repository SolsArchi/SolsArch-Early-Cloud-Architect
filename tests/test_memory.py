import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import helpers

import memory_harness as mh


def run(root, *args):
    script = str(helpers.SCRIPTS / "memory_harness.py")
    done = subprocess.run([sys.executable, "-I", script, "--path", str(root), *args],
                          capture_output=True, text=True)
    return done.returncode, json.loads(done.stdout)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_then_set_then_read_across_processes(self):
        code, out = run(self.root, "read")
        self.assertEqual((code, out["status"]), (0, "empty"))
        code, out = run(self.root, "set", "--project-name", "rag-platform", "--primary-cloud", "AWS",
                        "--target-region", "eu-west-1", "--compliance", "SOC2, GDPR", "--budget", "450")
        self.assertEqual(code, 0)
        code, out = run(self.root, "read")
        self.assertEqual(out["status"], "found")
        self.assertEqual(out["data"]["primary_cloud"], "aws")
        self.assertEqual(out["data"]["compliance_frameworks"], ["SOC2", "GDPR"])
        self.assertIn("rag-platform", out["message"])
        self.assertIn("eu-west-1", out["message"])

    def test_assumed_values_are_flagged_until_confirmed(self):
        run(self.root, "set", "--primary-cloud", "aws", "--target-region", "eu-west-1", "--assumed")
        out = run(self.root, "read")[1]
        self.assertEqual(out["data"]["assumed_fields"], ["primary_cloud", "target_region"])
        self.assertIn("assumed", out["message"])
        run(self.root, "set", "--primary-cloud", "aws")
        out = run(self.root, "read")[1]
        self.assertEqual(out["data"]["assumed_fields"], ["target_region"])
        run(self.root, "set", "--target-region", "eu-west-1")
        out = run(self.root, "read")[1]
        self.assertEqual(out["data"]["assumed_fields"], [])
        self.assertNotIn("assumed", out["message"])

    def test_adr_lifecycle(self):
        code, out = run(self.root, "adr", "--component", "Database", "--decision", "Move to Aurora",
                        "--rationale", "Multi-replica", "--status", "approved")
        self.assertEqual((code, out["adr"]["id"], out["adr"]["status"]), (0, "ADR-001", "APPROVED"))
        code, out = run(self.root, "adr", "--component", "Ingress", "--decision", "Add ALB")
        self.assertEqual(out["adr"]["id"], "ADR-002")
        self.assertEqual(out["adr"]["status"], "PROPOSED")
        code, out = run(self.root, "adr", "--id", "ADR-002", "--status", "APPROVED")
        self.assertEqual(out["action"], "updated")
        code, out = run(self.root, "adr", "--component", "Database", "--decision", "Use Neon",
                        "--supersedes", "ADR-001")
        self.assertEqual(out["adr"]["id"], "ADR-003")
        data = run(self.root, "read")[1]["data"]
        old = next(d for d in data["architectural_decisions"] if d["id"] == "ADR-001")
        self.assertEqual((old["status"], old["superseded_by"]), ("SUPERSEDED", "ADR-003"))

    def test_validation_errors_do_not_write(self):
        code, out = run(self.root, "adr", "--component", "X")
        self.assertEqual((code, out["status"]), (1, "error"))
        code, out = run(self.root, "adr", "--component", "X", "--decision", "Y", "--status", "maybe")
        self.assertEqual(code, 1)
        code, out = run(self.root, "adr", "--id", "bad", "--status", "APPROVED")
        self.assertEqual(code, 1)
        code, out = run(self.root, "set")
        self.assertEqual(code, 1)
        code, out = run(self.root, "set", "--budget", "-5")
        self.assertEqual(code, 1)
        self.assertFalse(mh.memory_path(self.root).exists())

    def test_corrupt_file_is_moved_aside(self):
        path = mh.memory_path(self.root)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        code, out = run(self.root, "read")
        self.assertEqual(out["status"], "empty")
        self.assertIn("unreadable", out["warning"])
        backups = list(path.parent.glob("memory.json.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "{not json")
        code, out = run(self.root, "set", "--project-name", "fresh")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["project_name"], "fresh")

    def test_no_temp_files_left_behind(self):
        run(self.root, "set", "--project-name", "a")
        leftovers = [p.name for p in (self.root / ".solsarch").iterdir() if p.name != "memory.json"]
        self.assertEqual(leftovers, [])

    def test_scan_record_and_diff(self):
        scan_one = {"components": [{"category": "database", "name": "SQLite"}], "findings": [{"id": "SEC-003"}]}
        scan_two = {"components": [{"category": "database", "name": "PostgreSQL"}], "findings": []}
        first = self.root / "scan1.json"
        second = self.root / "scan2.json"
        first.write_text(json.dumps(scan_one), encoding="utf-8")
        second.write_text(json.dumps(scan_two), encoding="utf-8")
        code, out = run(self.root, "diff", "--scan-file", str(first))
        self.assertEqual(out["status"], "no-baseline")
        self.assertEqual(run(self.root, "record-scan", "--scan-file", str(first))[0], 0)
        code, out = run(self.root, "diff", "--scan-file", str(second))
        self.assertEqual(out["components_added"], ["database:PostgreSQL"])
        self.assertEqual(out["components_removed"], ["database:SQLite"])
        self.assertEqual(out["findings_resolved"], ["SEC-003"])
        self.assertEqual(run(self.root, "read")[1]["data"]["last_analyzed"][-1], "Z")

    def test_bad_scan_file(self):
        bad = self.root / "bad.json"
        bad.write_text("[]", encoding="utf-8")
        self.assertEqual(run(self.root, "record-scan", "--scan-file", str(bad))[0], 1)
        self.assertEqual(run(self.root, "record-scan", "--scan-file", str(self.root / "nope.json"))[0], 1)

    def test_alias_script(self):
        script = str(helpers.SCRIPTS / "hermes_memory.py")
        done = subprocess.run([sys.executable, "-I", script, "--path", str(self.root), "read"],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["status"], "empty")


if __name__ == "__main__":
    unittest.main()
