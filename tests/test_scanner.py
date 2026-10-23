import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import helpers
from helpers import write

import ast_scanner


def names(result, category):
    return {c["name"] for c in result["components"] if c["category"] == category}


def finding_ids(result):
    return {f["id"] for f in result["findings"]}


class ScannerTests(unittest.TestCase):
    def scan(self, builder):
        with tempfile.TemporaryDirectory() as tmp:
            builder(tmp)
            return ast_scanner.scan(tmp)

    def test_saas_components_and_evidence(self):
        result = self.scan(helpers.build_saas)
        self.assertEqual(names(result, "framework"), {"Next.js", "React"})
        self.assertIn("PostgreSQL", names(result, "database"))
        self.assertIn("Redis", names(result, "cache"))
        self.assertIn("BullMQ", names(result, "queue"))
        postgres = next(c for c in result["components"] if c["name"] == "PostgreSQL")
        self.assertTrue(all(e["file"] and e["line"] >= 1 for e in postgres["evidence"]))
        manifest = next(e for e in postgres["evidence"] if e["file"] == "package.json")
        self.assertEqual(manifest["line"], 6)
        self.assertEqual(len(result["infrastructure"]["dockerfiles"]), 1)
        self.assertTrue(result["infrastructure"]["dockerfiles"][0]["runs_as_root"])
        self.assertIn("SEC-002", finding_ids(result))
        sec2 = next(f for f in result["findings"] if f["id"] == "SEC-002")
        self.assertEqual(sec2["severity"], "medium")

    def test_rag_findings(self):
        result = self.scan(helpers.build_rag)
        ids = finding_ids(result)
        self.assertIn("SEC-003", ids)
        self.assertIn("SEC-004", ids)
        self.assertIn("SEC-005", ids)
        sec3 = next(f for f in result["findings"] if f["id"] == "SEC-003")
        self.assertEqual(sec3["severity"], "high")
        self.assertIn("SQLite", names(result, "database"))
        self.assertIn("FastAPI", names(result, "framework"))
        self.assertIn("OpenAI SDK", names(result, "ai"))

    def test_secret_values_never_printed(self):
        result = self.scan(helpers.build_rag)
        self.assertNotIn(helpers.fake_aws_key(), json.dumps(result))

    def test_terraform_findings(self):
        result = self.scan(helpers.build_terraform)
        ids = finding_ids(result)
        self.assertEqual({"SEC-001", "SEC-002", "SEC-005"} - ids, set())
        sec2 = next(f for f in result["findings"] if f["id"] == "SEC-002")
        self.assertEqual(sec2["severity"], "critical")
        self.assertEqual(result["provider_hints"].get("aws"), 4)

    def test_load_balancer_clears_sec001(self):
        def build(root):
            write(root, "main.tf", 'resource "aws_cloudfront_distribution" "c" {\n}\n'
                  'resource "aws_ecs_service" "s" {\n}\nresource "aws_lb" "l" {\n}\n')
        self.assertNotIn("SEC-001", finding_ids(self.scan(build)))

    def test_proxy_with_good_settings_is_clean(self):
        def build(root):
            helpers.build_rag(root)
            write(root, "deploy/nginx.conf", "location / {\n  proxy_pass http://app;\n"
                  "  proxy_buffering off;\n  proxy_read_timeout 300s;\n}\n")
        self.assertNotIn("SEC-004", finding_ids(self.scan(build)))

    def test_placeholders_are_not_secrets(self):
        def build(root):
            write(root, "settings.py", 'API_KEY = "your-api-key-goes-here1"\n'
                  'password = "${DB_PASSWORD}12345"\n'
                  'DATABASE_URL = "postgres://app:postgres@db:5432/app"\n')
        self.assertNotIn("SEC-005", finding_ids(self.scan(build)))

    def test_database_url_password_flagged(self):
        def build(root):
            write(root, "settings.py", 'DATABASE_URL = "postgres://app:' + 'Zx9f' + 'Q2kLm8@db:5432/app"' + chr(10))
        result = self.scan(build)
        self.assertIn("SEC-005", finding_ids(result))
        self.assertNotIn("Zx9fQ2kLm8", json.dumps(result))

    def test_env_file_respects_gitignore(self):
        def tracked(root):
            write(root, ".env", "STRIPE_SECRET=Abcdef1234567890xyz\n")
        def ignored(root):
            tracked(root)
            write(root, ".gitignore", ".env\n")
        self.assertIn("SEC-005", finding_ids(self.scan(tracked)))
        self.assertNotIn("SEC-005", finding_ids(self.scan(ignored)))

    def test_test_files_are_downgraded(self):
        def build(root):
            write(root, "app/main.py", "print('hi')\n")
            write(root, "tests/test_keys.py", 'KEY = "%s"\n' % helpers.fake_aws_key())
            write(root, "tests/test_db.py", "import sqlite3\n")
            write(root, "Dockerfile", "FROM python:3.12\nUSER app\n")
        result = self.scan(build)
        sec5 = next(f for f in result["findings"] if f["id"] == "SEC-005")
        self.assertEqual(sec5["severity"], "low")
        self.assertIn("test files", sec5["detail"])
        self.assertNotIn("SEC-003", finding_ids(result))
        sqlite = next(c for c in result["components"] if c["name"] == "SQLite")
        self.assertTrue(all(e.get("test_file") for e in sqlite["evidence"]))

    def test_real_secret_outranks_test_secret(self):
        def build(root):
            write(root, "tests/test_keys.py", 'KEY = "%s"\n' % helpers.fake_aws_key())
            write(root, "zzz/prod.py", 'KEY = "%s"\n' % helpers.fake_aws_key())
        sec5 = next(f for f in self.scan(build)["findings"] if f["id"] == "SEC-005")
        self.assertEqual(sec5["severity"], "critical")
        self.assertEqual(sec5["evidence"][0]["file"], "zzz/prod.py")

    def test_import_only_streaming_is_inferred(self):
        def weak(root):
            helpers.build_rag(root)
        def strong(root):
            helpers.build_rag(root)
            write(root, "app/main.py", "from fastapi.responses import StreamingResponse\n"
                  "def f():\n    return StreamingResponse(gen())\n")
        sec4 = lambda r: next(f for f in r["findings"] if f["id"] == "SEC-004")
        self.assertEqual(sec4(self.scan(weak))["confidence"], "inferred")
        self.assertEqual(sec4(self.scan(strong))["confidence"], "confirmed")
        self.assertTrue(all(f["confidence"] for f in self.scan(weak)["findings"]))

    def test_output_flag_and_no_absolute_path(self):
        script = str(helpers.SCRIPTS / "ast_scanner.py")
        with tempfile.TemporaryDirectory() as tmp:
            helpers.build_saas(tmp)
            target = Path(tmp) / ".solsarch" / "scan.json"
            done = subprocess.run([sys.executable, "-I", script, tmp, "--output", str(target)],
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(data["root"], Path(tmp).name)
            self.assertNotIn(str(Path(tmp).parent), json.dumps(data))
            self.assertEqual(json.loads(done.stdout)["written"], str(target))

    def test_skips_vendor_dirs_and_binaries(self):
        def build(root):
            write(root, "node_modules/pg/package.json", '{"dependencies": {"pg": "1"}}')
            (Path(root) / "blob.json").write_bytes(b"\0\1\2" * 100)
        result = self.scan(build)
        self.assertEqual(result["stats"]["files_scanned"], 0)
        self.assertEqual(result["components"], [])

    def test_broken_python_file_does_not_crash(self):
        def build(root):
            write(root, "bad.py", "def (:\n  import redis\n")
        self.assertEqual(self.scan(build)["components"], [])

    def test_max_files_truncates(self):
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(5):
                write(tmp, "f%d.py" % index, "x = 1\n")
            result = ast_scanner.scan(tmp, max_files=2)
        self.assertTrue(result["stats"]["truncated"])
        self.assertEqual(result["stats"]["files_scanned"], 2)

    def test_speed_on_small_repo(self):
        result = self.scan(helpers.build_saas)
        self.assertLess(result["stats"]["elapsed_ms"], 500)

    def test_cli_outputs_json_and_rejects_bad_path(self):
        script = str(helpers.SCRIPTS / "ast_scanner.py")
        with tempfile.TemporaryDirectory() as tmp:
            helpers.build_saas(tmp)
            done = subprocess.run([sys.executable, "-I", script, tmp], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stdout)["scanner_version"], ast_scanner.SCANNER_VERSION)
        missing = subprocess.run([sys.executable, "-I", script, "no-such-dir-xyz"],
                                 capture_output=True, text=True)
        self.assertEqual(missing.returncode, 2)


if __name__ == "__main__":
    unittest.main()
