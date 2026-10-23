"""Secrets that were committed once and removed later are still found, and their values are never kept."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
import ast_scanner

GIT = shutil.which("git")


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false"] + list(args),
                   check=True, capture_output=True)


@unittest.skipUnless(GIT, "git is not installed")
class History(unittest.TestCase):
    def repo(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        root = Path(t.name)
        git(root, "init", "-q")
        return root

    def test_a_removed_secret_is_reported_without_its_value(self):
        root = self.repo()
        key = "AKIA" + "ABCDEFGHIJKLMNOP"
        (root / "config.py").write_text('AWS_KEY = "%s"\n' % key, encoding="utf-8")
        git(root, "add", "."); git(root, "commit", "-q", "-m", "add")
        (root / "config.py").write_text("AWS_KEY = None\n", encoding="utf-8")
        git(root, "add", "."); git(root, "commit", "-q", "-m", "remove")
        scan = ast_scanner.scan(root)
        self.assertEqual(scan["history"]["status"], "ran")
        found = [f for f in scan["findings"] if f["id"] == "SEC-006"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "critical")
        self.assertNotIn(key, json.dumps(scan))

    def test_a_repository_without_secrets_reports_a_clean_history(self):
        root = self.repo()
        (root / "app.py").write_text("print('hello')\n", encoding="utf-8")
        git(root, "add", "."); git(root, "commit", "-q", "-m", "add")
        scan = ast_scanner.scan(root)
        self.assertEqual(scan["history"]["status"], "ran")
        self.assertFalse([f for f in scan["findings"] if f["id"] == "SEC-006"])

    def test_placeholders_are_not_secrets(self):
        root = self.repo()
        (root / ".env.example").write_text("API_KEY=your-api-key-here\n", encoding="utf-8")
        git(root, "add", "."); git(root, "commit", "-q", "-m", "add")
        self.assertFalse([f for f in ast_scanner.scan(root)["findings"] if f["id"] == "SEC-006"])


class NoRepository(unittest.TestCase):
    def test_without_git_it_says_not_run_and_never_clean(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "app.py").write_text("print(1)\n", encoding="utf-8")
            scan = ast_scanner.scan(t)
            self.assertEqual(scan["history"]["status"], "not_run")
            self.assertTrue(scan["history"]["reason"])


if __name__ == "__main__":
    unittest.main()
