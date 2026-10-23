"""Security regressions: what happens when the repository SolsArch reads is hostile, and when the web or git hands it something unexpected.

A repository is untrusted input. The tool writes scripts and Terraform that a person later runs, so a folder named `x;rm -rf ~` or a
package called `${file("/etc/passwd")}` must stay data. These tests build such a repository and check everything that is generated.
"""
import ast
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import helpers  # noqa: F401  (puts the scripts folder on sys.path)
import agent_tools
import architect
import ast_scanner
import history_scan
import iac
import iac_common
import providers
import recommend
import safe_http
from helpers import write

SCRIPTS = Path(helpers.__file__).resolve().parent.parent / "solsarch-early-cloud-architect" / "scripts"
TOKENS = ("touch PWN", "`id`", "${file(", "<img src=x")


def hostile_repo(root):
    """Services in folders and packages whose names carry shell, Terraform and HTML payloads."""
    write(root, "api;touch PWN1/requirements.txt", "fastapi\nuvicorn\npymongo\nredis\n")
    write(root, "api;touch PWN1/main.py",
          "import os\nfrom fastapi import FastAPI\napp = FastAPI()\nX = os.environ['TOKEN_X']\n@app.get('/health')\ndef h():\n    return 1\n")
    write(root, "api;touch PWN1/Dockerfile", "FROM python:3.12\nEXPOSE 8000\nCMD [\"uvicorn\",\"main:app\"]\n")
    write(root, "web$(touch PWN2)/package.json",
          json.dumps({"name": "../../evil\";touch PWN3;\"${file(\"/etc/hostname\")}", "dependencies": {"next": "14.2.0", "react": "18"}}))
    write(root, "web$(touch PWN2)/app/page.tsx", "export default function(){return null}\n")
    write(root, "worker `id`/package.json", json.dumps({"name": "<img src=x onerror=alert(1)>", "dependencies": {"bullmq": "5"}}))
    write(root, "worker `id`/index.js", "const { Queue } = require('bullmq');\n")


def payload_lines(text):
    for number, line in enumerate(text.splitlines(), 1):
        if any(token in line for token in TOKENS):
            yield number, line


def shell_problems(path, text):
    """Lines where a payload from the repository would become part of the command instead of staying one quoted word."""
    found = []
    runs = []
    if path.endswith(".sh"):
        runs = [(n, line) for n, line in payload_lines(text)]
    elif path.endswith(".yml") and "workflows" in path:
        for n, line in payload_lines(text):
            m = re.search(r'run: "(.*)"\s*$', line)
            if m:
                runs.append((n, json.loads('"' + m.group(1) + '"')))
    elif path.endswith(".md"):
        in_fence = False
        for n, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("```"):
                in_fence = not in_fence
            elif in_fence and any(token in line for token in TOKENS):
                runs.append((n, line))
    for n, body in runs:
        for part in body.split("\n"):
            if not any(token in part for token in TOKENS):
                continue
            try:
                words = shlex.split(part)
            except ValueError:
                found.append((path, n, "quotes do not balance: " + part[:100]))
                continue
            if any(w.startswith("touch") or w.startswith("PWN") or w == "id" for w in words):
                found.append((path, n, "the payload became a separate shell word: " + part[:100]))
    return found


def other_problems(path, text):
    found = []
    if path.endswith((".tf", ".hcl")):
        for n, line in payload_lines(text):
            if "${file(" in line and "$${file(" not in line:
                found.append((path, n, "an HCL expression from the repository would run: " + line[:100]))
    elif path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return [(path, 0, "generated python does not parse")]
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("exec", "eval", "system"):
                found.append((path, node.lineno, "dangerous call in generated python"))
    elif path.endswith(".json"):
        try:
            json.loads(text)
        except ValueError:
            found.append((path, 0, "invalid json"))
    elif path.endswith(".toml"):
        try:
            tomllib.loads(text)
        except ValueError:
            found.append((path, 0, "invalid toml"))
    return found


class HostileRepository(unittest.TestCase):
    """Everything generated for a repository whose names are payloads."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.root = cls.tmp / "it's & proj"
        cls.root.mkdir()
        hostile_repo(cls.root)
        cls.scan = ast_scanner.scan(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def build(self, provider, mode):
        profile = dict(architect.PROFILE_DEFAULTS, account_type="standard")
        plan = recommend.build_plan(self.scan, recommend.load_catalog(), provider, mode, [], profile)
        design = architect.review(self.root, self.scan, plan, profile)
        return iac.build(plan, design, profile, self.scan, "it's & proj")

    def test_no_provider_turns_a_repository_name_into_a_command(self):
        for provider in providers.hosting_ids():
            if provider in ("neon", "mongodbatlas"):
                continue
            for mode in ("server", "serverless"):
                with self.subTest(provider=provider, mode=mode):
                    result = self.build(provider, mode)
                    problems = []
                    for path, text in result["files"].items():
                        problems += shell_problems(path, text) + other_problems(path, text)
                    self.assertEqual(problems, [])

    def test_unit_names_are_safe_identifiers(self):
        for unit in architect.review(self.root, self.scan, recommend.build_plan(
                self.scan, recommend.load_catalog(), "aws", "server", [], dict(architect.PROFILE_DEFAULTS)), dict(architect.PROFILE_DEFAULTS))["units"]:
            self.assertRegex(unit["name"], r"^[a-z0-9][a-z0-9-]*$")

    def test_generated_paths_stay_inside_the_output_folder(self):
        result = self.build("aws", "server")
        for path in result["files"]:
            self.assertFalse(path.startswith("/") or ".." in path.split("/"), path)

    def test_the_scan_never_ran_anything_from_the_repository(self):
        self.assertEqual([p for p in self.tmp.rglob("PWN*") if p.is_file()], [])


class OutputFolderGuards(unittest.TestCase):
    def test_a_file_path_cannot_leave_the_output_folder(self):
        files = iac_common.Files("abc")
        for bad in ("../x.tf", "a/../../x.tf", "/etc/x", "C:/x", ""):
            with self.subTest(path=bad):
                with self.assertRaises(ValueError):
                    files.add(bad, "x")

    def test_writing_refuses_a_path_outside_the_folder(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ValueError):
                iac.write({"../escape.txt": "x"}, Path(t) / "out")
            self.assertFalse((Path(t) / "escape.txt").exists())


class Quoting(unittest.TestCase):
    def test_shq_keeps_one_word(self):
        for value in ("a b", "x;touch y", "$(id)", "`id`", "it's", 'say "hi"', "a\nb", ""):
            self.assertEqual(shlex.split(iac_common.shq(value)), [value])

    def test_hcl_strings_cannot_open_an_expression(self):
        text = iac_common.hcl_str('a${file("/etc/passwd")}b%{if true}c"d\\e')
        self.assertNotIn("${file", text.replace("$${", ""))
        self.assertNotIn("%{if", text.replace("%%{", ""))
        self.assertNotIn('"d', text.replace('\\"', ""))


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class GitIsNotTrusted(unittest.TestCase):
    """Reading history must not run programs that the repository's own git settings name."""

    def make(self, root):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")

        def git(*args):
            subprocess.run(["git", "-C", str(root), "-c", "commit.gpgsign=false"] + list(args), check=True, capture_output=True, env=env)
        git("init", "-q")
        (root / "notes.txt").write_text("one\n", encoding="utf-8")
        git("add", ".")
        git("commit", "-q", "-m", "one")
        (root / "notes.txt").write_text("two\n", encoding="utf-8")
        git("add", ".")
        git("commit", "-q", "-m", "two")
        return git

    def test_a_text_conversion_program_named_by_the_repository_is_not_run(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / "repo"
            root.mkdir()
            marker = Path(t) / "RAN"
            script = Path(t) / "convert.py"
            script.write_text("import sys, pathlib\npathlib.Path(%r).write_text('ran')\nsys.stdout.write(open(sys.argv[1]).read())\n" % str(marker), encoding="utf-8")
            self.make(root)
            (root / ".gitattributes").write_text("*.txt diff=evil\n", encoding="utf-8")
            config = root / ".git" / "config"
            config.write_text(config.read_text(encoding="utf-8") + '\n[diff "evil"]\n\ttextconv = "%s" "%s"\n' % (sys.executable.replace("\\", "/"), str(script).replace("\\", "/")),
                              encoding="utf-8")
            result = history_scan.scan(root, ast_scanner.SECRET_PATTERNS, ast_scanner.GENERIC_ASSIGN_RE, ast_scanner.DB_URL_RE, ast_scanner.is_placeholder)
            self.assertEqual(result["status"], "ran")
            self.assertFalse(marker.exists(), "git ran a conversion program that only the repository named")

    def test_the_environment_given_to_git_is_cut_down(self):
        os.environ["AWS_SECRET_ACCESS_KEY"] = "not-a-real-key"
        try:
            env = history_scan.safe_env()
        finally:
            os.environ.pop("AWS_SECRET_ACCESS_KEY", None)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")


class WebRequests(unittest.TestCase):
    def test_only_https_is_opened(self):
        for good in ("https://example.com/x",):
            self.assertEqual(safe_http.checked(good), good)
        for bad in ("http://example.com/x", "file:///etc/passwd", "ftp://example.com/x", "gopher://x", "https:///nohost", ""):
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    safe_http.checked(bad)

    def test_plain_http_is_allowed_only_for_a_model_server_on_this_machine(self):
        self.assertEqual(safe_http.checked("http://localhost:11434/v1", allow_loopback_http=True), "http://localhost:11434/v1")
        with self.assertRaises(ValueError):
            safe_http.checked("http://example.com/v1", allow_loopback_http=True)

    def redirect(self, source, target, headers):
        handler = safe_http.SafeRedirects()
        request = urllib.request.Request(source, headers=headers)
        return handler.redirect_request(request, io.BytesIO(), 302, "Found", {}, target)

    def test_a_redirect_to_plain_http_is_refused(self):
        with self.assertRaises(urllib.error.URLError):
            self.redirect("https://api.example.com/a", "http://api.example.com/b", {})

    def test_a_credential_does_not_follow_a_redirect_to_another_host(self):
        new = self.redirect("https://api.vendor.example/a", "https://elsewhere.example/b", {"Authorization": "Bearer secret", "Accept": "x"})
        self.assertNotIn("Authorization", {k.title() for k in new.headers} | {k.title() for k in new.unredirected_hdrs})
        same = self.redirect("https://api.vendor.example/a", "https://api.vendor.example/b", {"Authorization": "Bearer secret"})
        self.assertIn("Authorization", {k.title() for k in same.headers} | {k.title() for k in same.unredirected_hdrs})


class AgentTools(unittest.TestCase):
    def test_a_value_that_looks_like_an_option_is_refused(self):
        code, payload = agent_tools.call("solsarch_scan", {"repo": "--help"})
        self.assertEqual(code, 2)
        code, payload = agent_tools.call("solsarch_scan", {"repo": ".", "surprise": "x"})
        self.assertEqual(code, 2)


class GeneratedPipelines(unittest.TestCase):
    def test_every_third_party_action_is_pinned_to_a_commit_and_nothing_pipes_a_download_into_a_shell(self):
        import iac_ci
        for provider in ("aws", "azure", "gcp", "hetzner", "vultr"):
            with self.subTest(provider=provider):
                from test_iac import build
                files = build(provider, "server")[4]["files"]
                for path, text in files.items():
                    if ".github/workflows" not in path:
                        continue
                    for line in text.splitlines():
                        m = re.search(r"uses: (\S+)@(\S+)", line)
                        if m:
                            self.assertRegex(m.group(2), r"^[0-9a-f]{40}$", line)
                    self.assertNotRegex(text, r"curl[^\n]*\|\s*(sh|bash)")
                    self.assertNotIn("persist-credentials: true", text)
        self.assertRegex(iac_ci.SKILL_REF, r"^v\d+\.\d+\.\d+$")

    def test_the_pinned_actions_table_has_full_commits(self):
        import iac_ci
        for name, (tag, sha) in iac_ci.PINS.items():
            self.assertRegex(sha, r"^[0-9a-f]{40}$", name)
            self.assertTrue(tag, name)


class TheToolItself(unittest.TestCase):
    def test_no_shell_eval_or_exec_anywhere_in_the_scripts(self):
        bad = []
        for path in sorted(SCRIPTS.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name in ("eval", "exec", "system", "popen"):
                        bad.append((path.name, node.lineno, name))
                    for kw in node.keywords:
                        if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                            bad.append((path.name, node.lineno, "shell=True"))
        self.assertEqual(bad, [])

    def test_the_repository_holds_nothing_that_looks_like_a_real_credential(self):
        root = SCRIPTS.parent.parent
        tracked = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True)
        if tracked.returncode != 0:
            self.skipTest("not a git checkout")
        patterns = [rx for _, rx in ast_scanner.SECRET_PATTERNS]
        hits = []
        for name in tracked.stdout.decode("utf-8").split("\0"):
            path = root / name
            if not name or path.suffix in (".png", ".zip", ".pptx", ".pdf", ".svg") or not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if any(rx.search(line) for rx in patterns):
                    hits.append("%s:%d" % (name, n))
        self.assertEqual(hits, [])

    def test_the_zips_checked_into_examples_hold_no_credential(self):
        root = SCRIPTS.parent.parent
        patterns = [rx for _, rx in ast_scanner.SECRET_PATTERNS]
        for z in sorted((root / "examples").rglob("*.zip")):
            with zipfile.ZipFile(z) as archive:
                for name in archive.namelist():
                    if name.endswith("/"):
                        continue
                    text = archive.read(name).decode("utf-8", "ignore")
                    self.assertFalse(any(rx.search(text) for rx in patterns), "%s in %s" % (name, z.name))


if __name__ == "__main__":
    unittest.main()
