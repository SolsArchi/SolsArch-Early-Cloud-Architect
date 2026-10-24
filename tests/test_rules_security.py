"""Rules documents are untrusted input: nothing in them may reach a shell, a Terraform string or a path unchecked."""
import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
from helpers import write
import rules
import rules_extract as rx

HOSTILE_TAGS = ['x"; rm -rf /', "a$(whoami)", "t${var.secret}", "a b c d e", "../etc", "ok\nbad", "`id`", "x" * 80]


class Values(unittest.TestCase):
    def test_tag_keys_with_metacharacters_are_rejected(self):
        for bad in HOSTILE_TAGS:
            with self.subTest(bad):
                value, problem = rx.normalise("required_tags", [bad])
                self.assertIsNone(value)
                self.assertTrue(problem)

    def test_prefix_region_and_numbers_are_checked(self):
        for kind, bad in (("naming_prefix", 'a"b'), ("naming_prefix", "x$(id)"), ("allowed_regions", 'eu"west'), ("allowed_regions", "a b"),
                          ("budget_monthly", "1e999"), ("budget_monthly", "nan"), ("rpo_minutes", "0"), ("log_retention_min_days", "-3")):
            with self.subTest((kind, bad)):
                value, problem = rx.normalise(kind, bad)
                self.assertIsNone(value, (kind, bad))

    def test_prose_cannot_smuggle_a_hostile_tag(self):
        hits = rx.prose_rules('Every resource must carry these tags: owner, x"; rm -rf /, cost-center.')
        names = [v for k, v, _ in hits if k == "required_tags"]
        for group in names:
            for name in group:
                self.assertRegex(name, rx.TAG_KEY.pattern)

    def test_generated_code_only_contains_validated_tag_keys(self):
        data = {"constraints": {"required_tags": ["owner", "cost-center"], "naming_prefix": "acme"}}
        files = {"terraform/providers.tf": 'provider "aws" {\n  default_tags {\n    tags = {\n      A = "b"\n    }\n  }\n}\n',
                 "terraform/terraform.tfvars.example": 'project_name = "shop"\n'}
        out, _ = rules.apply_to_files(files, data, "aws")
        self.assertIn("merge(var.company_tags", out["terraform/providers.tf"])
        self.assertIn('"cost-center"', out["terraform/company-tags.tf"])
        self.assertIn('project_name = "acme', out["terraform/terraform.tfvars.example"])

    def test_rego_does_not_let_a_sentence_break_out_of_its_string(self):
        rule = {"id": "R-no-public-database-aaaaaa", "kind": "no_public_database", "value": True, "level": "blocking", "status": "active",
                "source": {"file": "a.md", "locator": "line 1", "sentence": 'say "hi" \\ and ${evil}', "sha256": "0"}}
        text = rules.rego({"rules": [rule]})
        line = next(l for l in text.splitlines() if "R-no-public-database-aaaaaa" in l)
        self.assertNotIn('"hi"', line)
        self.assertNotIn(chr(92), line)


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_rules_file_cannot_make_the_tool_write_outside_the_repo(self):
        write(self.root, "policies/evil.json", json.dumps({"rules": [{"kind": "naming_prefix", "value": "../../zzhostile"}]}))
        data = rules.build(self.root)
        self.assertEqual(data["rules"], [])
        self.assertTrue(data["problems"])
        self.assertEqual([p.name for p in self.root.parent.glob("zzhostile")], [])

    def test_symlinked_rules_are_not_followed(self):
        outside = tempfile.TemporaryDirectory()
        try:
            secret = Path(outside.name) / "secret.md"
            secret.write_text("Data must stay in the EU.\n", encoding="utf-8")
            (self.root / "policies").mkdir()
            try:
                (self.root / "policies" / "link.md").symlink_to(secret)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not available")
            self.assertIsNone(rules.build(self.root))
        finally:
            outside.cleanup()

    def test_secret_looking_text_in_a_rules_file_is_not_executed_or_printed_as_a_rule_value(self):
        write(self.root, "policies/a.md", "Databases must not be public. password=hunter2hunter2\n")
        data = rules.build(self.root)
        self.assertEqual([r["kind"] for r in data["rules"]], ["no_public_database"])
        self.assertTrue(all(r["value"] is True for r in data["rules"]))

    def test_markdown_report_escapes_table_bars(self):
        write(self.root, "policies/a.md", "Databases must not be public | really.\n")
        data = rules.build(self.root)
        report = rules.render_markdown(data)
        row = next(l for l in report.splitlines() if l.startswith("| R-no-public-database"))
        self.assertEqual(row.count(" | "), 5)


if __name__ == "__main__":
    unittest.main()
