import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import helpers

ROOT = helpers.SKILL_DIR.parent
INSTALL = str(ROOT / "install.py")


def run(*args, cwd=None):
    return subprocess.run([sys.executable, "-B", INSTALL, *map(str, args)],
                          capture_output=True, text=True, cwd=cwd)


class InstallTests(unittest.TestCase):
    def test_install_to_dest_then_refuse_then_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = run("--dest", tmp)
            self.assertEqual(first.returncode, 0, first.stderr)
            installed = Path(tmp) / "solsarch-early-cloud-architect"
            self.assertTrue((installed / "SKILL.md").exists())
            self.assertTrue((installed / "scripts" / "build_report.py").exists())
            self.assertEqual(list(installed.rglob("__pycache__")), [])
            again = run("--dest", tmp)
            self.assertEqual(again.returncode, 1)
            self.assertIn("--force", again.stderr)
            (installed / "stale.txt").write_text("x", encoding="utf-8")
            self.assertEqual(run("--dest", tmp, "--force").returncode, 0)
            self.assertFalse((installed / "stale.txt").exists())

    def test_project_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = run("--target", "project", cwd=tmp)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue((Path(tmp) / ".claude" / "skills" / "solsarch-early-cloud-architect" / "SKILL.md").exists())

    def test_installed_copy_validates_and_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            run("--dest", tmp)
            installed = Path(tmp) / "solsarch-early-cloud-architect"
            checked = subprocess.run([sys.executable, "-I", "-B", str(installed / "scripts" / "validate_skill.py"),
                                      str(installed)], capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stdout)
            repo = Path(tmp) / "repo"
            helpers.build_saas(repo)
            scanned = subprocess.run([sys.executable, "-I", "-B", str(installed / "scripts" / "ast_scanner.py"), str(repo)],
                                     capture_output=True, text=True)
            self.assertEqual(scanned.returncode, 0, scanned.stderr)

    def test_zip_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dist" / "skill.zip"
            done = run("--zip", target)
            self.assertEqual(done.returncode, 0, done.stderr)
            with zipfile.ZipFile(target) as archive:
                names = archive.namelist()
                self.assertIsNone(archive.testzip())
            self.assertIn("solsarch-early-cloud-architect/SKILL.md", names)
            self.assertTrue(all(n.startswith("solsarch-early-cloud-architect/") for n in names))
            self.assertFalse(any("__pycache__" in n or n.endswith(".pyc") for n in names))
            self.assertFalse(any(n.lower().endswith("readme.md") for n in names))

    def test_zip_is_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            run("--zip", Path(tmp) / "a.zip")
            run("--zip", Path(tmp) / "b.zip")
            self.assertEqual((Path(tmp) / "a.zip").read_bytes(), (Path(tmp) / "b.zip").read_bytes())


if __name__ == "__main__":
    unittest.main()
