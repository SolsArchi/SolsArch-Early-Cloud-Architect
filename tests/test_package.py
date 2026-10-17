import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import helpers

import diagram_sanitizer as ds
import validate_skill

SKILL = helpers.SKILL_DIR


class PackageTests(unittest.TestCase):
    def test_skill_is_valid(self):
        self.assertEqual(validate_skill.validate(SKILL), [])

    def test_validator_catches_problems(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "My Skill"
            bad.mkdir()
            helpers.write(bad, "skill.md", "---\nname: My Skill\n---\nbody\n")
            self.assertTrue(any("SKILL.md not found" in p for p in validate_skill.validate(bad)))
            worse = Path(tmp) / "other-name"
            worse.mkdir()
            helpers.write(worse, "SKILL.md", "---\nname: claude-helper\ndescription: Does things <b>\n---\nSee `references/x.md`\n")
            helpers.write(worse, "README.md", "x")
            problems = " | ".join(validate_skill.validate(worse))
            for expected in ("reserved word", "does not match the folder", "angle brackets",
                             "README.md must not", "references/x.md", "when to use"):
                self.assertIn(expected, problems)

    def test_validator_cli(self):
        script = str(helpers.SCRIPTS / "validate_skill.py")
        done = subprocess.run([sys.executable, "-I", script, str(SKILL)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(json.loads(done.stdout)["valid"])

    def test_description_budget(self):
        fields, body, error = validate_skill.parse_frontmatter((SKILL / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIsNone(error)
        self.assertLessEqual(len(fields["description"]), 1024)
        self.assertLess(len(body.split("\n")), 500)

    def test_reference_diagrams_pass_sanitizer(self):
        checked = 0
        for path in sorted((SKILL / "references").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            cleaned, report = ds.sanitize_markdown(text)
            for item in report:
                checked += 1
                self.assertEqual(item["issues"], [], "%s block %d" % (path.name, item["block"]))
            self.assertEqual(ds.sanitize_markdown(cleaned)[0], cleaned, "%s is not stable" % path.name)
        self.assertGreaterEqual(checked, 10)

    def test_json_assets_parse(self):
        for relative in ("assets/cloud-vendor-palette.json", "evals/evals.json"):
            json.loads((SKILL / relative).read_text(encoding="utf-8"))
        palette = json.loads((SKILL / "assets/cloud-vendor-palette.json").read_text(encoding="utf-8"))
        for line in palette["mermaid_classdefs"]:
            self.assertRegex(line, r"^classDef \w+ fill:#[0-9A-F]{6},stroke:#[0-9A-F]{6},color:#[0-9A-F]{6}$")

    def test_skill_md_names_every_script_it_uses(self):
        body = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        for command in ("solsarch.py questions", "solsarch.py scan", "solsarch.py plan", "solsarch.py facts queue",
                        "solsarch.py facts add", "solsarch.py facts skip", "solsarch.py finish"):
            self.assertIn(command, body)
            self.assertIn("scripts/solsarch.py", body)
        for removed in ("region, budget", "--budget", "ast_scanner.py REPO"):
            self.assertNotIn(removed, body.replace("not region, not budget", ""))

    def test_skill_asks_only_the_two_questions(self):
        body = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Ask exactly the questions", body)
        self.assertIn("server", body)
        self.assertIn("serverless", body)

    def test_eval_trigger_sets(self):
        evals = json.loads((SKILL / "evals/evals.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(evals["trigger_queries"]["should_trigger"]), 20)
        self.assertGreaterEqual(len(evals["trigger_queries"]["should_not_trigger"]), 10)
        self.assertGreaterEqual(len(evals["functional_cases"]), 3)

    def test_no_forbidden_files_in_skill(self):
        names = {p.name.lower() for p in SKILL.rglob("*") if p.is_file()}
        self.assertNotIn("readme.md", names)


if __name__ == "__main__":
    unittest.main()
